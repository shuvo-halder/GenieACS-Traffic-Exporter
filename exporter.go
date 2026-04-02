// exporter.go
//
// GenieACS -> VictoriaMetrics push exporter (no Prometheus required).
// Polls GenieACS NBI, computes per-device bytes and rates, and POSTs
// Prometheus text exposition to VictoriaMetrics /api/v1/import/prometheus.
//
// Env vars:
//  GENIEACS_API_URL (default http://genieacs-nbi:7557)
//  POLL_INTERVAL (seconds, default 30)
//  DEVICE_QUERY_FILTER (JSON query for /devices, default {})
//  BATCH_SIZE (devices per batch, default 200)
//  WORKERS (concurrent device workers, default 100)
//  STALE_SECONDS (offline threshold, default 300)
//  VM_INGEST_URL (VictoriaMetrics ingest endpoint, default http://victoriametrics:8428/api/v1/import/prometheus)
//  SHARD_COUNT (optional sharding total, default 1)
//  SHARD_INDEX (optional shard index, default 0)
//  PARAM_PATHS (comma separated param patterns, optional override)
//  GENIEACS_API_USER / GENIEACS_API_PASS (optional basic auth for GenieACS)
//  VM_AUTH_HEADER (optional extra header for VM ingestion, e.g., "Authorization: Bearer ...")
package main

import (
    "bytes"
    "context"
    "encoding/json"
    "errors"
    "flag"
    "fmt"
    "io"
    "log"
    "math"
    "net"
    "net/http"
    "net/url"
    "os"
    "strconv"
    "strings"
    "sync"
    "time"
)

type deviceListEntry struct {
    ID string `json:"_id"`
}

type deviceObject struct {
    ID         string      `json:"_id"`
    LastInform interface{} `json:"lastInform"`
}

type paramEntry struct {
    Name  string      `json:"name"`
    Value interface{} `json:"value"`
}

type deviceState struct {
    prevRx uint64
    prevTx uint64
    prevT  time.Time
    mu     sync.Mutex
}

type Exporter struct {
    apiURL       string
    vmIngestURL  string
    client       *http.Client
    pollInterval time.Duration
    deviceQuery  string
    batchSize    int
    workers      int
    staleSeconds int
    paramPaths   []string
    stateMu      sync.RWMutex
    state        map[string]*deviceState
    sem          chan struct{}
    shardCount   int
    shardIndex   int
    authUser     string
    authPass     string
    vmAuthHeader string
}

func getenv(key, def string) string {
    if v := os.Getenv(key); v != "" {
        return v
    }
    return def
}

func getenvInt(key string, def int) int {
    if v := os.Getenv(key); v != "" {
        if i, err := strconv.Atoi(v); err == nil {
            return i
        }
    }
    return def
}

func NewExporter() *Exporter {
    apiURL := strings.TrimRight(getenv("GENIEACS_API_URL", "http://genieacs-nbi:7557"), "/")
    vmURL := getenv("VM_INGEST_URL", "http://victoriametrics:8428/api/v1/import/prometheus")
    poll := time.Duration(getenvInt("POLL_INTERVAL", 30)) * time.Second
    deviceQuery := getenv("DEVICE_QUERY_FILTER", "{}")
    batchSize := getenvInt("BATCH_SIZE", 200)
    workers := getenvInt("WORKERS", 100)
    stale := getenvInt("STALE_SECONDS", 300)
    shardCount := getenvInt("SHARD_COUNT", 1)
    shardIndex := getenvInt("SHARD_INDEX", 0)
    paramPathsEnv := os.Getenv("PARAM_PATHS")
    var paramPaths []string
    if paramPathsEnv != "" {
        for _, p := range strings.Split(paramPathsEnv, ",") {
            if s := strings.TrimSpace(p); s != "" {
                paramPaths = append(paramPaths, s)
            }
        }
    } else {
        paramPaths = []string{
            "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Stats.*.EthernetBytesReceived",
            "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Stats.*.EthernetBytesSent",
            "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.Stats.BytesReceived",
            "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.Stats.BytesSent",
        }
    }

    transport := &http.Transport{
        DialContext: (&net.Dialer{
            Timeout:   10 * time.Second,
            KeepAlive: 30 * time.Second,
        }).DialContext,
        MaxIdleConns:        500,
        IdleConnTimeout:     90 * time.Second,
        TLSHandshakeTimeout: 10 * time.Second,
    }

    return &Exporter{
        apiURL:       apiURL,
        vmIngestURL:  vmURL,
        client:       &http.Client{Transport: transport, Timeout: 30 * time.Second},
        pollInterval: poll,
        deviceQuery:  deviceQuery,
        batchSize:    batchSize,
        workers:      workers,
        staleSeconds: stale,
        paramPaths:   paramPaths,
        state:        make(map[string]*deviceState),
        sem:          make(chan struct{}, workers),
        shardCount:   shardCount,
        shardIndex:   shardIndex,
        authUser:     os.Getenv("GENIEACS_API_USER"),
        authPass:     os.Getenv("GENIEACS_API_PASS"),
        vmAuthHeader: os.Getenv("VM_AUTH_HEADER"),
    }
}

func (e *Exporter) getJSON(ctx context.Context, path string, out interface{}) error {
    u := e.apiURL + path
    req, err := http.NewRequestWithContext(ctx, "GET", u, nil)
    if err != nil {
        return err
    }
    req.Header.Set("Accept", "application/json")
    if e.authUser != "" {
        req.SetBasicAuth(e.authUser, e.authPass)
    }
    resp, err := e.client.Do(req)
    if err != nil {
        return err
    }
    defer resp.Body.Close()
    if resp.StatusCode >= 400 {
        body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
        return fmt.Errorf("http %d: %s", resp.StatusCode, string(body))
    }
    dec := json.NewDecoder(resp.Body)
    return dec.Decode(out)
}

func (e *Exporter) fetchDevices(ctx context.Context) ([]string, error) {
    q := url.QueryEscape(e.deviceQuery)
    path := fmt.Sprintf("/devices/?query=%s", q)
    var list []deviceListEntry
    if err := e.getJSON(ctx, path, &list); err != nil {
        return nil, err
    }
    ids := make([]string, 0, len(list))
    for _, d := range list {
        if d.ID != "" {
            // optional sharding by hash
            if e.shardCount > 1 {
                h := fnv32(d.ID)
                if int(h%uint32(e.shardCount)) != e.shardIndex {
                    continue
                }
            }
            ids = append(ids, d.ID)
        }
    }
    return ids, nil
}

func fnv32(s string) uint32 {
    const (
        offset32 = 2166136261
        prime32  = 16777619
    )
    h := uint32(offset32)
    for i := 0; i < len(s); i++ {
        h ^= uint32(s[i])
        h *= prime32
    }
    return h
}

func (e *Exporter) fetchDeviceObject(ctx context.Context, id string) (*deviceObject, error) {
    var obj deviceObject
    if err := e.getJSON(ctx, "/devices/"+url.PathEscape(id), &obj); err != nil {
        return nil, err
    }
    return &obj, nil
}

func (e *Exporter) fetchParameters(ctx context.Context, id string) ([]paramEntry, error) {
    var params []paramEntry
    path := fmt.Sprintf("/devices/%s/parameters/?query={}", url.PathEscape(id))
    if err := e.getJSON(ctx, path, &params); err != nil {
        return nil, err
    }
    return params, nil
}

func parseLastInform(v interface{}) time.Time {
    if v == nil {
        return time.Time{}
    }
    switch t := v.(type) {
    case float64:
        sec := int64(t)
        if sec > 1e12 {
            return time.Unix(0, sec*int64(time.Millisecond))
        }
        if sec > 1e9 {
            return time.Unix(sec, 0)
        }
        return time.Time{}
    case string:
        if i, err := strconv.ParseInt(t, 10, 64); err == nil {
            if i > 1e12 {
                return time.Unix(0, i*int64(time.Millisecond))
            }
            if i > 1e9 {
                return time.Unix(i, 0)
            }
        }
        if tm, err := time.Parse(time.RFC3339, t); err == nil {
            return tm
        }
        return time.Time{}
    case int64:
        if t > 1e12 {
            return time.Unix(0, t*int64(time.Millisecond))
        }
        if t > 1e9 {
            return time.Unix(t, 0)
        }
        return time.Time{}
    default:
        return time.Time{}
    }
}

func (e *Exporter) extractBytes(params []paramEntry) (uint64, uint64) {
    var rx uint64
    var tx uint64
    for _, p := range params {
        name := strings.ToLower(p.Name)
        var valUint uint64
        switch v := p.Value.(type) {
        case float64:
            if v < 0 {
                continue
            }
            valUint = uint64(v)
        case string:
            if v == "" {
                continue
            }
            if parsed, err := strconv.ParseUint(v, 10, 64); err == nil {
                valUint = parsed
            } else {
                continue
            }
        case int:
            if v < 0 {
                continue
            }
            valUint = uint64(v)
        case int64:
            if v < 0 {
                continue
            }
            valUint = uint64(v)
        default:
            continue
        }
        if strings.HasSuffix(name, "bytesreceived") || strings.HasSuffix(name, "ethernetbytesreceived") {
            if valUint > rx {
                rx = valUint
            }
        }
        if strings.HasSuffix(name, "bytessent") || strings.HasSuffix(name, "ethernetbytessent") {
            if valUint > tx {
                tx = valUint
            }
        }
    }
    return rx, tx
}

func computeRate(prev uint64, curr uint64, dt float64) float64 {
    if dt <= 0 {
        return 0
    }
    if curr >= prev {
        delta := curr - prev
        return float64(delta*8) / dt
    }
    // wrap handling
    const max32 = uint64(math.MaxUint32)
    const max64 = uint64(math.MaxUint64)
    var delta uint64
    if prev <= max32 && curr <= max32 {
        delta = curr + (max32 + 1) - prev
    } else {
        delta = curr + (max64 + 1) - prev
    }
    return float64(delta*8) / dt
}

func (e *Exporter) processDevice(ctx context.Context, id string, now time.Time, buf *bytes.Buffer, bufMu *sync.Mutex) {
    // concurrency limiter
    e.sem <- struct{}{}
    defer func() { <-e.sem }()

    obj, err := e.fetchDeviceObject(ctx, id)
    if err != nil {
        log.Printf("device fetch error %s: %v", id, err)
        return
    }
    lastInform := parseLastInform(obj.LastInform)
    if lastInform.IsZero() {
        return
    }
    if now.Sub(lastInform) > time.Duration(e.staleSeconds)*time.Second {
        return
    }
    params, err := e.fetchParameters(ctx, id)
    if err != nil {
        log.Printf("params fetch error %s: %v", id, err)
        return
    }
    rx, tx := e.extractBytes(params)

    // update state
    e.stateMu.Lock()
    ds, ok := e.state[id]
    if !ok {
        ds = &deviceState{}
        e.state[id] = ds
    }
    e.stateMu.Unlock()

    ds.mu.Lock()
    var rxbps, txbps float64
    if !ds.prevT.IsZero() {
        dt := now.Sub(ds.prevT).Seconds()
        rxbps = computeRate(ds.prevRx, rx, dt)
        txbps = computeRate(ds.prevTx, tx, dt)
    } else {
        rxbps = 0
        txbps = 0
    }
    ds.prevRx = rx
    ds.prevTx = tx
    ds.prevT = now
    ds.mu.Unlock()

    // Build Prometheus text exposition lines for VictoriaMetrics ingestion
    // Use labels: device_id
    // Metrics: genieacs_device_rx_bytes, genieacs_device_tx_bytes, genieacs_device_rx_rate_bps, genieacs_device_tx_rate_bps
    nowMs := now.UnixNano() / int64(time.Millisecond)
    lines := []string{
        fmt.Sprintf("genieacs_device_rx_bytes{device_id=%q} %d %d", id, rx, nowMs),
        fmt.Sprintf("genieacs_device_tx_bytes{device_id=%q} %d %d", id, tx, nowMs),
        fmt.Sprintf("genieacs_device_rx_rate_bps{device_id=%q} %f %d", id, rxbps, nowMs),
        fmt.Sprintf("genieacs_device_tx_rate_bps{device_id=%q} %f %d", id, txbps, nowMs),
    }
    bufMu.Lock()
    for _, l := range lines {
        buf.WriteString(l)
        buf.WriteByte('\n')
    }
    bufMu.Unlock()
}

func (e *Exporter) pushToVM(ctx context.Context, payload []byte) error {
    if len(payload) == 0 {
        return nil
    }
    req, err := http.NewRequestWithContext(ctx, "POST", e.vmIngestURL, bytes.NewReader(payload))
    if err != nil {
        return err
    }
    req.Header.Set("Content-Type", "text/plain; version=0.0.4")
    if e.vmAuthHeader != "" {
        // vmAuthHeader expected like "Authorization: Bearer <token>"
        parts := strings.SplitN(e.vmAuthHeader, ":", 2)
        if len(parts) == 2 {
            req.Header.Set(strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1]))
        }
    }
    resp, err := e.client.Do(req)
    if err != nil {
        return err
    }
    defer resp.Body.Close()
    if resp.StatusCode >= 300 {
        body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
        return fmt.Errorf("vm ingest http %d: %s", resp.StatusCode, string(body))
    }
    return nil
}

func (e *Exporter) runOnce(ctx context.Context) error {
    now := time.Now()
    ids, err := e.fetchDevices(ctx)
    if err != nil {
        return err
    }
    if len(ids) == 0 {
        return nil
    }
    var wg sync.WaitGroup
    buf := &bytes.Buffer{}
    var bufMu sync.Mutex

    for i := 0; i < len(ids); i += e.batchSize {
        end := i + e.batchSize
        if end > len(ids) {
            end = len(ids)
        }
        batch := ids[i:end]
        for _, id := range batch {
            wg.Add(1)
            go func(d string) {
                defer wg.Done()
                e.processDevice(ctx, d, now, buf, &bufMu)
            }(id)
        }
        wg.Wait()
        // after each batch, push to VM to avoid huge payloads
        payload := buf.Bytes()
        if len(payload) > 0 {
            // retry with backoff
            var lastErr error
            for attempt := 0; attempt < 3; attempt++ {
                if err := e.pushToVM(ctx, payload); err != nil {
                    lastErr = err
                    time.Sleep(time.Duration(500*(attempt+1)) * time.Millisecond)
                    continue
                }
                lastErr = nil
                break
            }
            if lastErr != nil {
                log.Printf("failed to push batch to VM: %v", lastErr)
            }
            // reset buffer
            buf.Reset()
        }
    }
    // final push if any
    if buf.Len() > 0 {
        if err := e.pushToVM(ctx, buf.Bytes()); err != nil {
            log.Printf("final push error: %v", err)
        }
    }
    return nil
}

func (e *Exporter) Run(ctx context.Context) {
    ticker := time.NewTicker(e.pollInterval)
    defer ticker.Stop()
    for {
        select {
        case <-ctx.Done():
            return
        default:
        }
        start := time.Now()
        if err := e.runOnce(ctx); err != nil {
            log.Printf("poll error: %v", err)
        }
        elapsed := time.Since(start)
        // sleep until next tick but ensure at least 1s gap
        select {
        case <-ctx.Done():
            return
        case <-time.After(maxDuration(1*time.Second, e.pollInterval-elapsed)):
        }
    }
}

func maxDuration(a, b time.Duration) time.Duration {
    if a > b {
        return a
    }
    return b
}

func main() {
    listen := flag.String("listen", getenv("LISTEN_ADDR", ":9105"), "listen address for health")
    flag.Parse()

    exp := NewExporter()

    // simple health endpoint
    http.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
        w.WriteHeader(200)
        w.Write([]byte("ok"))
    })
    go func() {
        log.Printf("health server listening on %s", *listen)
        if err := http.ListenAndServe(*listen, nil); err != nil && !errors.Is(err, http.ErrServerClosed) {
            log.Fatalf("health server: %v", err)
        }
    }()

    ctx := context.Background()
    log.Printf("starting exporter: polling %s every %s -> %s", exp.apiURL, exp.pollInterval, exp.vmIngestURL)
    exp.Run(ctx)
}
