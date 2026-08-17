# KQL behind the panels

One file per SLI, named for its catalogue ID. Every query returns the same
shape — `good`, `valid` (or `numerator`, `denominator`) — so `slo.py` can
evaluate any indicator without knowing what it measures.

Parameters are declared with `declare query_parameters` so the same file serves
the dashboard panel, the Azure Monitor rule, and `make slo-report` without
three divergent copies drifting apart.
