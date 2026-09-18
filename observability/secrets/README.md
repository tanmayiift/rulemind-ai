# Alert destination

`paging_url` holds the single URL Alertmanager POSTs alerts to. It is **gitignored**;
`paging_url.example` is the committed template.

```bash
cp observability/secrets/paging_url.example observability/secrets/paging_url
# put your real webhook URL in it, one line, no quotes
docker compose --profile observability restart alertmanager
```

Anything that accepts a POST works — a Slack incoming webhook, a PagerDuty Events v2 endpoint,
an Opsgenie webhook, or your own receiver.

**Why a file and not an environment variable.** Alertmanager does not expand env vars in its
config: `${SOMETHING}` is validated as a literal URL and fails the whole config with
`unsupported scheme ""`. It is a file because that is the only mechanism that works, not as a
matter of taste.

**Why not committed with a placeholder.** `url_file` is read at NOTIFY time rather than at load
time, so a missing file does not stop Alertmanager starting — it logs a delivery failure when an
alert actually fires. Loud at the moment it matters beats a config that looks wired and silently
drops everything, which is what this replaced.
