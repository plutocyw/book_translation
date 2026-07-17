You are identifying a book from extracted PDF text for a Traditional Chinese translation project.

Infer conservatively from the PDF evidence, then verify the likely title, author, genre, intended audience, edition, and ISBN with authoritative online sources when web search is available. Do not invent an ISBN, edition, author, or publication fact. When a field is uncertain, use null and explain the uncertainty. Return strict JSON only:

{"title":"...","author":"...","genre":"...","audience":"...","source_edition":null,"isbn":null,"confidence":"high|medium|low","notes":"..."}
