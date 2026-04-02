# Build stage
FROM golang:1.20-alpine AS build
WORKDIR /src
RUN apk add --no-cache git build-base
COPY go.mod go.sum ./
RUN go mod download || true
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /bin/exporter ./exporter.go

# Runtime stage
FROM alpine:3.18
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/exporter /bin/exporter
USER 1000
EXPOSE 9105
ENTRYPOINT ["/bin/exporter"]
