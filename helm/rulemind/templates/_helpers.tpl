{{/*
Shared snippets. Previously every template repeated itself (or, in the case of
securityContext, simply had none) — a cluster enforcing the `restricted` Pod
Security Standard rejected all three Deployments and the migration Job outright.
*/}}

{{/* Pod-level security context. Include with: {{- include "rulemind.podSecurityContext" . | nindent 6 }} */}}
{{- define "rulemind.podSecurityContext" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: {{ .Values.securityContext.runAsUser }}
  runAsGroup: {{ .Values.securityContext.runAsGroup }}
  fsGroup: {{ .Values.securityContext.fsGroup }}
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{/* Container-level security context. Include with: {{- include "rulemind.containerSecurityContext" . | nindent 10 }} */}}
{{- define "rulemind.containerSecurityContext" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: {{ .Values.securityContext.readOnlyRootFilesystem }}
  runAsNonRoot: true
  runAsUser: {{ .Values.securityContext.runAsUser }}
  capabilities:
    drop:
      - ALL
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{/*
CORS origins for the API. env.corsOrigins wins; otherwise derive both schemes
from the ingress host so a default `helm install` does not fall back to the
localhost-only default baked into app/main.py.
*/}}
{{- define "rulemind.corsOrigins" -}}
{{- if .Values.env.corsOrigins -}}
{{ .Values.env.corsOrigins }}
{{- else -}}
https://{{ .Values.ingress.host }},http://{{ .Values.ingress.host }}
{{- end -}}
{{- end -}}
