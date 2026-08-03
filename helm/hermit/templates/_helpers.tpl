{{- define "hermit.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "hermit.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "hermit.labels" -}}
helm.sh/chart: {{ include "hermit.name" . }}-{{ .Chart.Version | replace "+" "_" }}
{{ include "hermit.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "hermit.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hermit.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}