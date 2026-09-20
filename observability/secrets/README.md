# Alert destinations

Three files, all gitignored, all read at **notify** time rather than at load time — so adding one
works without a restart, and removing one fails loudly on the next alert instead of silently.

| File | Who reads it | What it is |
|---|---|---|
| `topic` | `alert-relay` | the ntfy topic real alerts are pushed to |
| `heartbeat_topic` | `alert-relay` | the ntfy topic the Watchdog heartbeat goes to — **not** the on-call one |
| `watchdog_url` | `watchdog-monitor` | optional external dead-man's-switch, for the one case a local monitor cannot cover |
| `paging_url` | *(retired)* | superseded by `topic` + the relay |

```bash
cp observability/secrets/topic.example        observability/secrets/topic
cp observability/secrets/watchdog_url.example observability/secrets/watchdog_url
# edit both, then:
docker compose --profile observability restart alertmanager alert-relay
bash scripts/verify_paging.sh --send
```

---

## `topic` — where real alerts go

An ntfy topic name. **The name is the secret**: anyone who knows it can read your alerts and
publish to them, which is why it lives here and not in the repo. Make it unguessable:

```bash
printf 'rulemind-%s' "$(python3 -c 'import secrets; print(secrets.token_hex(8))')" \
  > observability/secrets/topic
```

Then install the ntfy app (iOS/Android) and subscribe to that topic. `alert-relay` turns
Alertmanager's envelope into a notification with a title, a priority taken from `severity`, and
a tappable runbook — see `observability/alert-relay/relay.py` for why that layer exists.

**For production, self-host it.** Alert summaries name your policies and your instances; on
ntfy.sh they pass through someone else's server. ntfy is open source and the server is one
container — set `RULEMIND_NTFY_BASE` to your own and nothing else here changes.

---

## `heartbeat_topic` — where the heartbeat goes

Its own topic, never the on-call one. A heartbeat delivered where a human is subscribed sends
"nothing is wrong" every five minutes, which is how a topic gets muted and stops being watched.

```bash
printf 'rulemind-hb-%s' "$(python3 -c 'import secrets; print(secrets.token_hex(8))')" \
  > observability/secrets/heartbeat_topic
```

`watchdog-monitor` polls this topic for **silence** and pages the on-call topic when it stops.
**Verified doing so**: stopping Alertmanager produced `[ALERTING IS DOWN] no heartbeat` at
priority 5 within the grace period, and restoring it produced `[RECOVERED] heartbeat is back`.

It covers five of the six ways alerting dies — a dead relay, a broken Alertmanager reload, a
misrouted receiver, Prometheus stopping, a container not returning. The sixth, the whole host
going away, it structurally cannot, because it runs on that host. That is what `watchdog_url`
below is for.

**The grace must exceed Alertmanager's `repeat_interval`** (5m), or the monitor pages during
normal operation. It refuses to start otherwise — found the honest way, by testing a 90s grace
and watching it page immediately after an Alertmanager restart.

---

## `watchdog_url` — the external backstop

**Optional, and it covers exactly one thing the local monitor cannot: this host going away.**

`watchdog-monitor` pings this URL **only while it can see heartbeats arriving**. That ordering is
the design — ping it unconditionally and it reports healthy while the pipeline it vouches for is
dead. Pinging only on a verified heartbeat means its silence says "the local chain stopped
proving itself", which includes the monitor and the machine.

Configure the receiving end to alert if it has not heard from you in **15 minutes** — three
missed beats, so one slow delivery does not page anyone.

### Grafana OnCall (free tier)

1. Sign in at <https://grafana.com> → **My Account** → create a free Grafana Cloud stack if you
   have none.
2. In the stack, open **Alerts & IRM → OnCall → Integrations**.
3. **New integration → Alertmanager**. Name it `rulemind-heartbeat`.
4. Open the integration → **Heartbeat** (right-hand panel or the ⋯ menu).
5. Set the **interval to 15 minutes** and copy the **heartbeat URL**. It looks like
   `https://oncall-prod-<region>.grafana.net/oncall/integrations/v1/alertmanager/<token>/heartbeat/`
6. Paste it into `observability/secrets/watchdog_url` — the whole URL, one line, no quotes.
7. Add yourself to the integration's **escalation chain** (OnCall → Escalation Chains), or a
   missed heartbeat raises an alert group nobody is notified about.

```bash
docker compose --profile observability restart alertmanager
bash scripts/verify_paging.sh          # all links green
```

Within ~5 minutes OnCall shows the heartbeat as **green/received**. To prove the *absence*
detection — the only part that matters — stop Alertmanager and wait out the interval:

```bash
docker compose --profile observability stop alertmanager   # heartbeat stops
# ~15 min later OnCall should fire "heartbeat expired" and escalate to you
docker compose --profile observability start alertmanager
```

**Do that once.** A dead man's switch nobody has ever seen fire is a dead man's switch you are
trusting on faith, and the whole point of it is that it works on the day nothing else does.

### Others, same shape

Any endpoint that records a ping and alerts on its absence works — Opsgenie Heartbeats,
healthchecks.io, Better Stack. Set the expected period to 15 minutes and put the URL in the file.

### Self-hosting the monitor

Grafana OnCall is open source, so you can run it yourself — but **not on this host**, or you have
rebuilt the fate-sharing problem. Put it in a different failure domain: another machine, another
cloud account, another region. If it can die in the same event as the API, it is not a dead
man's switch.
