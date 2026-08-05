# Standalone Rust decide service — the high-throughput /decide hot path.
# Multi-stage: build a static-ish release binary, then run it on a slim base.
FROM rust:1-slim AS build
WORKDIR /build
# Copy both crates (the service depends on the core via a path dependency).
COPY packages/rulemind-core-rs ./packages/rulemind-core-rs
COPY packages/rulemind-decide-service ./packages/rulemind-decide-service
WORKDIR /build/packages/rulemind-decide-service
RUN cargo build --release

FROM debian:bookworm-slim
RUN useradd -r -u 10001 rulemind
COPY --from=build /build/packages/rulemind-decide-service/target/release/rulemind-decide-service /usr/local/bin/rulemind-decide-service
USER rulemind
ENV RULEMIND_DECIDE_ADDR=0.0.0.0:8090
EXPOSE 8090
# Mount or bake the compiled bundle and point RULEMIND_BUNDLE_PATH at it.
ENTRYPOINT ["/usr/local/bin/rulemind-decide-service"]
