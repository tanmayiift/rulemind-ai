# No dependencies on purpose: relay.py is stdlib only. A paging relay that needs a package index
# to start is one more thing between an alert and a human.
FROM python:3.11-slim
WORKDIR /app
COPY relay.py .
RUN adduser --system --uid 10002 relay
USER relay
EXPOSE 9097
CMD ["python3", "-u", "relay.py"]
