# Health Score

The Health Score provides a single metric to understand the quality and reliability of an asset, schema, or the entire warehouse.

## Scoring Logic

The system subtracts points from a base score of 100 based on the presence of OPEN findings. The penalty depends on the `FindingType`.

Current default weights:
- `QUALITY_ISSUE`: -20
- `SCHEMA_DRIFT`: -15
- `SEMANTIC_DUPLICATE`: -10
- `ARCHITECTURE_FLAG`: -10
- `COST_INEFFICIENCY`: -5
- `ROOT_CAUSE` / `RECOMMENDED_FIX`: -0

### Scope Aggregation
- **Asset Scope**: The score is calculated for a single asset.
- **Schema Scope**: The score is the average of all child asset scores in the schema. We also store `min_child_score` (the lowest individual asset score among the children) to power a per-dashboard trust indicator.
- **Global Scope**: The score is the average of all child asset scores globally. We also store `min_child_score`.

## Limitations

### v1 Limitation: Severity is Per Finding Type Only
Currently, severity is penalized purely by the `finding_type` (e.g., any `QUALITY_ISSUE` costs -20). It is *not* scaled by the finding's actual magnitude (e.g., a small null-rate tick and a severe one both cost -20). 

Evidence for scaling this later already exists in `Finding.evidence`, but it is not used yet. Future iterations should scale the penalty based on the magnitude of the issue.
