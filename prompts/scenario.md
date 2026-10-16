You specify a reproducible inventory-table evaluation case for the observed task.
Return one JSON object with keys description, task, question, generation, requested_capability.
Return raw JSON without Markdown fences. The task must use only metric, an optional
single filter_field/filter_value string pair, and an optional group_by string.
Do not include null fields. group_by is a string such as "warehouse", never a list.
Example task: {"metric":"available","group_by":"warehouse"}.
Generation must include integer seed, row_count from 1 to 1024, nonempty distinct
warehouses/categories, and on_hand [positive_minimum, positive_maximum].
Choose data that makes the observed failure reproducible. Do not invent an expected answer.
If the request needs another metric, data source, filter operation, or oracle, return
{"contract_extension":{"behavior":"...","data_access":"...","oracle":"..."}}.
