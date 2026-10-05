# Legacy OpenAPI specs

Frozen copies of public specs that left the docs catalog when the API moved to
the canonical conversation vocabulary. The API still serves every path in them
(see https://docs.coval.ai/api-reference/v1/conversations-compatibility), so
the SDKs keep generating these surfaces:

| File | Paths | SDK surface |
|---|---|---|
| `conversations-v1.yaml` | `/conversations`, `/conversations:submit` | `ConversationsApi` (`client.conversations`), `AudioApi.get_conversation_audio` |
| `simulations-v1.yaml` | `/simulations`, `/simulations:rerunMetrics` | `SimulationsApi` (`client.simulations`), `MetricOutputsApi.simulations_list_metrics` / `simulations_get_metric` |
| `monitors-v1.yaml` | `/monitors` | `MonitorsApi`, `MonitorEventsApi` (`client.monitors`, `client.monitor_events`) |

Source: `coval-ai/docs@4aa9515202a3e841e5429cd9f6d685b1d440cff2`
(`api-reference/v1/`), the last revision the weekly regeneration published
before the rename. The files are byte-for-byte copies; do not edit them.

`scripts/bundle-spec.mjs` merges them after the canonical specs. Canonical
specs win: a legacy path or tag definition a canonical spec also defines is
dropped from the legacy copy. Legacy operations keep their docs filename slug,
so conflict renames such as `simulations_listMetrics` match the published
method names.

New integrations should use the canonical `UploadedConversationsApi`,
`SimulatedConversationsApi`, and `AlertsApi`. Remove a legacy spec only in a
major SDK release, after the API retires its paths.
