# Dockerfile
FROM golang:1.20-alpine AS build
WORKDIR /src
RUN apk add --no-cache git build-base
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /bin/genieacs_exporter ./genieacs_exporter.go

FROM alpine:3.18
RUN apk add --no-cache ca-certificates
COPY --from=build /bin/genieacs_exporter /bin/genieacs_exporter
EXPOSE 9105
USER 1000
ENTRYPOINT ["/bin/genieacs_exporter"]
