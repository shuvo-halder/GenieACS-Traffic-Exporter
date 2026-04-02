// exporter.go
//
// Production-ready GenieACS -> Prometheus exporter in Go.
// Exposes per-device bytes and computed rates.
// Environment variables documented below.
package main

import (
    "context"
    "encoding/json"
    "flag"
    "fmt"
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

    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promhttp"
)

// Environment variables
// GENIEACS_API_URL - base URL for GenieACS NBI e.g. http://genieacs-nbi:7557
// POLL_INTERVAL - seconds between polls (default 30)
// DEVICE_QUERY_FILTER - URL encoded query for /devices endpoint (default {})
// BATCH_SIZE - number of devices to fetch details concurrently per batch (default 200)
// WORKERS - number of concurrent HTTP workers for parameter fetches (default 100)
// STALE_SECONDS - consider device offline if lastInform older than this (default 300)
// LISTEN_ADDR - exporter listen address (default :9105)
// PARAM_PATHS - comma separated parameter name patterns to look for (optional override)

var (
    // Prometheus metrics
    rxBytes = prometheus.NewGaugeVec(
        prometheus.GaugeOpts{
            Name: "genieacs_device_rx_bytes",
            Help: "Total received bytes reported by device parameter",
        },
        []string{"device_id"},
    )
    txBytes = prometheus.NewGaugeVec(
        prometheus.GaugeOpts{
            Name: "genieacs_device_tx_bytes",
            Help: "Total transmitted bytes reported by device parameter",
        },
        []string{"device_id"},
    )
    rxRate = prometheus.NewGaugeVec(
        prometheus.GaugeOpts{
            Name: "genieacs_device_rx_rate_bps",
            Help: "Computed receive rate in bits per second",
        },
        []string{"device_id"},
    )
    txRate = prometheus.NewGaugeVec(
        prometheus.GaugeOpts{
            Name: "genieacs_device_tx_rate_bps",
            Help: "Computed transmit rate in bits per second",
        },
        []string{"device_id"},
    )
)

func init() {
    prometheus.MustRegister(rxBytes, txBytes, rxRate, txRate)
}

// internal state per device
type deviceState struct {
    sync.Mutex
    prevRx uint64
    prevTx uint64
    prevT  time.Time
}

type exporter struct {
    apiURL        string
    client        *http.Client
    pollInterval  time.Duration
    deviceQuery   string
    batchSize     int
    workers       int
    staleSeconds  int
    paramPatterns []string

    stateMu sync.RWMutex
    state   map[string]*deviceState

    // concurrency control
    sem chan struct{}
}

func newExporter() *exporter {
    apiURL := getenv("GENIEACS_API_URL", "http://genieacs-nbi:7557")
    pollInterval := time.Duration(getenvInt("POLL_INTERVAL", 30)) * time.Second
    deviceQuery := getenv("DEVICE_QUERY_FILTER", "{}")
    batchSize := getenvInt("BATCH_SIZE", 200)
    workers := getenvInt("WORKERS", 100)
    staleSeconds := getenvInt("STALE_SECONDS", 300)
    paramPathsEnv := os.Getenv("PARAM_PATHS")
    var paramPatterns []string
    if paramPathsEnv != "" {
        for _, p := range strings.Split(paramPathsEnv, ",") {
            p = strings.TrimSpace(p)
            if p != "" {
                paramPatterns = append(paramPatterns, p)
            }
        }
    } else {
        paramPatterns = []string{
            "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Stats.*.EthernetBytesReceived",
            "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Stats.*.EthernetBytesSent",
            "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.Stats.BytesReceived",
            "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.Stats.BytesSent",
        }
    }

    // tuned HTTP client
    transport := &http.Transport{
        DialContext: (&net.Dialer{
            Timeout:   10 * time.Second,
            KeepAlive: 30 * time.Second,
        }).DialContext,
        MaxIdleConns:        200,
        IdleConnTimeout:     90 * time.Second,
        TLSHandshakeTimeout: 10 * time.Second,
    }

    return &exporter{
        apiURL:       strings.TrimRight(apiURL, "/"),
        client:       &http.Client{Transport: transport, Timeout: 30 * time.Second},
        pollInterval: pollInterval,
        deviceQuery:  deviceQuery,
        batchSize:    batchSize,
        workers:      workers,
        staleSeconds: staleSeconds,
        paramPatterns: paramPatterns,
        state:        make(map[string]*deviceState),
        sem:          make(chan struct{}, workers),
    }
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

// Helper to perform GET and decode JSON
func (e *exporter) getJSON(ctx context.Context, path string, out interface{}) error {
    u := e.apiURL + path
    req, err := http.NewRequestWithContext(ctx, "GET", u, nil)
    if err != nil {
        return err
    }
    // Accept JSON
    req.Header.Set("Accept", "application/json")
    resp, err := e.client.Do(req)
    if err != nil {
        return err
    }
    defer resp.Body.Close()
    if resp.StatusCode >= 400 {
        return fmt.Errorf("http %d for %s", resp.StatusCode, u)
    }
    dec := json.NewDecoder(resp.Body)
    return dec.Decode(out)
}

// Device list structure returned by GenieACS NBI /devices?query=...
// We only need _id from each entry
type deviceListEntry struct {
    ID string `json:"_id"`
}

// Full device object minimal fields
type deviceObject struct {
    ID         string      `json:"_id"`
    LastInform interface{} `json:"lastInform"`
}

// Parameter entry structure
type paramEntry struct {
    Name  string      `json:"name"`
    Value interface{} `json:"value"`
}

// fetchDevices queries /devices/?query=<deviceQuery>
// GenieACS returns array of devices; this function returns device IDs
func (e *exporter) fetchDevices(ctx context.Context) ([]string, error) {
    // deviceQuery may be JSON; ensure URL encoding
    q := url.QueryEscape(e.deviceQuery)
    path := fmt.Sprintf("/devices/?query=%s", q)
    var list []deviceListEntry
    if err := e.getJSON(ctx, path, &list); err != nil {
        return nil, err
    }
    ids := make([]string, 0, len(list))
    for _, d := range list {
        if d.ID != "" {
            ids = append(ids, d.ID)
        }
    }
    return ids, nil
}

// fetchDeviceObject fetches /devices/{id}
func (e *exporter) fetchDeviceObject(ctx context.Context, id string) (*deviceObject, error) {
    var obj deviceObject
    if err := e.getJSON(ctx, "/devices/"+url.PathEscape(id), &obj); err != nil {
        return nil, err
    }
    return &obj, nil
}

// fetchParameters fetches /devices/{id}/parameters/?query={}
func (e *exporter) fetchParameters(ctx context.Context, id string) ([]paramEntry, error) {
    var params []paramEntry
    path := fmt.Sprintf("/devices/%s/parameters/?query={}", url.PathEscape(id))
    if err := e.getJSON(ctx, path, &params); err != nil {
        return nil, err
    }
    return params, nil
}

// extract bytes from parameters using pattern matching on suffixes
func (e *exporter) extractBytes(params []paramEntry) (uint64, uint64) {
    var rx uint64
    var tx uint64
    for _, p := range params {
        name := p.Name
        // value may be string or number
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

        lower := strings.ToLower(name)
        if strings.HasSuffix(lower, "bytesreceived") || strings.HasSuffix(lower, "ethernetbytesreceived") {
            if valUint > rx {
                rx = valUint
            }
        }
        if strings.HasSuffix(lower, "bytessent") || strings.HasSuffix(lower, "ethernetbytessent") {
            if valUint > tx {
                tx = valUint
            }
        }
    }
    return rx, tx
}

// computeRate handles counter wrap and returns bits per second
func computeRate(prev uint64, curr uint64, dtSeconds float64) float64 {
    if dtSeconds <= 0 {
        return 0
    }
    if curr >= prev {
        delta := curr - prev
        return float64(delta*8) / dtSeconds
    }
    // counter wrapped. Try 64-bit then 32-bit fallback
    // If prev looks like 32-bit max, use 32-bit wrap
    const max32 = uint64(math.MaxUint32)
    const max64 = uint64(math.MaxUint64)
    var delta uint64
    // choose wrap size heuristically: if prev <= max32 and curr <= max32 assume 32-bit wrap
    if prev <= max32 && curr <= max32 {
        delta = curr + (max32 + 1) - prev
    } else {
        delta = curr + (max64 + 1) - prev
    }
    return float64(delta*8) / dtSeconds
}

// processDevice fetches device object and parameters, computes rates, updates metrics
func (e *exporter) processDevice(ctx context.Context, id string, now time.Time) {
    // concurrency limiter
    e.sem <- struct{}{}
    defer func() { <-e.sem }()

    // fetch device object
    obj, err := e.fetchDeviceObject(ctx, id)
    if err != nil {
        // log and skip
        log.Printf("device fetch error %s: %v", id, err)
        return
    }

    // determine lastInform and filter by STALE_SECONDS
    lastInform := parseLastInform(obj.LastInform)
    if lastInform.IsZero() {
        // no lastInform treat as offline
        return
    }
    if now.Sub(lastInform) > time.Duration(e.staleSeconds)*time.Second {
        // offline skip
        return
    }

    // fetch parameters
    params, err := e.fetchParameters(ctx, id)
    if err != nil {
        log.Printf("params fetch error %s: %v", id, err)
        return
    }

    rx, tx := e.extractBytes(params)
    // update state and compute rates
    e.stateMu.Lock()
    ds, ok := e.state[id]
    if !ok {
        ds = &deviceState{}
        e.state[id] = ds
    }
    e.stateMu.Unlock()

    ds.Lock()
    defer ds.Unlock()

    var rxbps, txbps float64
    if !ds.prevT.IsZero() {
        dt := now.Sub(ds.prevT).Seconds()
        rxbps = computeRate(ds.prevRx, rx, dt)
        txbps = computeRate(ds.prevTx, tx, dt)
    } else {
        rxbps = 0
        txbps = 0
    }

    // update previous
    ds.prevRx = rx
    ds.prevTx = tx
    ds.prevT = now

    // set metrics
    rxBytes.WithLabelValues(id).Set(float64(rx))
    txBytes.WithLabelValues(id).Set(float64(tx))
    rxRate.WithLabelValues(id).Set(rxbps)
    txRate.WithLabelValues(id).Set(txbps)
}

// parseLastInform handles different possible types returned by GenieACS
func parseLastInform(v interface{}) time.Time {
    if v == nil {
        return time.Time{}
    }
    switch t := v.(type) {
    case float64:
        // could be seconds or milliseconds
        sec := int64(t)
        // heuristic: if > 1e12 treat as ms
        if sec > 1e12 {
            return time.Unix(0, sec*int64(time.Millisecond))
        }
        // if > 1e9 treat as seconds
        if sec > 1e9 {
            return time.Unix(sec, 0)
        }
        // fallback
        return time.Time{}
    case string:
        // try parse as integer
        if i, err := strconv.ParseInt(t, 10, 64); err == nil {
            if i > 1e12 {
                return time.Unix(0, i*int64(time.Millisecond))
            }
            if i > 1e9 {
                return time.Unix(i, 0)
            }
        }
        // try RFC3339
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

// runPoll performs one full poll cycle
func (e *exporter) runPoll(ctx context.Context) {
    now := time.Now()
    ids, err := e.fetchDevices(ctx)
    if err != nil {
        log.Printf("failed to list devices: %v", err)
        return
    }
    if len(ids) == 0 {
        return
    }

    // process in batches to avoid huge concurrency spikes
    for i := 0; i < len(ids); i += e.batchSize {
        end := i + e.batchSize
        if end > len(ids) {
            end = len(ids)
        }
        batch := ids[i:end]
        var wg sync.WaitGroup
        for _, id := range batch {
            wg.Add(1)
            go func(did string) {
                defer wg.Done()
                e.processDevice(ctx, did, now)
            }(id)
        }
        wg.Wait()
    }
}

// background loop
func (e *exporter) run(ctx context.Context) {
    ticker := time.NewTicker(e.pollInterval)
    defer ticker.Stop()
    for {
        select {
        case <-ctx.Done():
            return
        default:
            e.runPoll(ctx)
        }
        select {
        case <-ctx.Done():
            return
        case <-ticker.C:
            // continue
        }
    }
}

func main() {
    // optional flags to override env
    listen := flag.String("listen", getenv("LISTEN_ADDR", ":9105"), "listen address")
    flag.Parse()

    exp := newExporter()

    // start HTTP server for Prometheus metrics
    http.Handle("/metrics", promhttp.Handler())
    server := &http.Server{Addr: *listen}

    ctx, cancel := context.WithCancel(context.Background())
    defer cancel()

    go func() {
        log.Printf("starting exporter on %s, polling %s every %s", *listen, exp.apiURL, exp.pollInterval)
        if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
            log.Fatalf("http server error: %v", err)
        }
    }()

    // run poll loop
    exp.run(ctx)

    // graceful shutdown (not reached in normal run)
    _ = server.Shutdown(ctx)
}
