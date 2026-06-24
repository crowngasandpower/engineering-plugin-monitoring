# claude-status-verb

Tiny AWS Lambda that backs the **Claude Status** panel on the Grafana
Infrastructure dashboard.

## What it does

Fetches `https://status.claude.com/api/v2/status.json` and returns a small JSON
array the Grafana [Infinity](https://grafana.com/grafana/plugins/yesoreyeram-infinity-datasource/)
panel consumes:

```json
[{"display": "Cogitating...", "indicator": "none"}]
```

- When Claude is **operational** (`indicator == "none"`), `display` is a random
  Claude Code spinner verb (the 185 built-in defaults from
  [wynandw87/claude-code-spinner-verbs](https://github.com/wynandw87/claude-code-spinner-verbs)).
  A new verb is picked on every invocation, i.e. every panel refresh.
- Outage states return a fixed label (`Minor Outage` / `Major Outage` /
  `Critical Outage`) so the panel's value-mappings still colour it yellow/red.
- On any fetch error it returns `Status Unknown`.

## Why a Lambda + API Gateway (not a Function URL)

The account blocks **public Lambda Function URLs** (an org guardrail — the URL
returns `Forbidden` even with a correct `*`/`NONE` resource policy). So the
function is exposed through an **API Gateway HTTP API** instead, which is the
sanctioned public ingress.

## Deployed resources (eu-west-2, acct 939490550781)

| Resource | Name / ID |
| --- | --- |
| Lambda | `claude-status-verb` (python3.12, 128 MB, 12 s) |
| Execution role | `claude-status-verb-role` (+ `AWSLambdaBasicExecutionRole`) |
| API Gateway (HTTP API) | `claude-status-verb-api` — `0ndrg8kla9` |
| Public endpoint | `https://0ndrg8kla9.execute-api.eu-west-2.amazonaws.com` |

The Grafana `claude-status-infinity` datasource's `allowedHosts` is locked to
that endpoint, and the panel queries it (see
`images/grafana/provisioning/datasources/datasources.yml` and the **Claude
Status** panel in `images/grafana/dashboards/Platform/infrastructure.json`).

## Redeploy the code

```bash
cd claude-status-lambda
python -c "import zipfile;z=zipfile.ZipFile('fn.zip','w',zipfile.ZIP_DEFLATED);z.write('handler.py');z.close()"
aws lambda update-function-code --function-name claude-status-verb \
  --zip-file fileb://fn.zip --region eu-west-2
```

To refresh the verb list, re-extract the "Built-in Default Verbs (185)" list
from the upstream README into `VERBS` in `handler.py`.
