You are performing terminology discovery for a book that will be translated into Traditional Chinese for Taiwan.

Extract only recurring or consistency-sensitive items: people, aliases, places, organizations, titles, ranks, invented terms, technical terms, works, and meaningful capitalized concepts. Do not extract ordinary vocabulary.

Return strict JSON only, with this shape:

{"terms":[{"source_term":"...","proposed_target":"...","category":"character|place|organization|title|technical|invented|work|concept|other","notes":"...","confidence":"high|medium|low"}]}

Use an empty array when there are no qualifying terms. Proposed targets must use Traditional Chinese. Treat proposals as provisional, not approved.
