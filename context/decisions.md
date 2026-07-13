# Translation decisions

Record consequential decisions with the date, affected term or passage, decision, and rationale. Approved decisions should also be reflected in `glossary.csv` or `characters.csv`.

## 2026-07-11 - Project identification and audience

- Decision: Treat the source as Richard A. Knaak's *Diablo: Legacy of Blood*, Pocket Books English edition, ISBN `0-7434-2312-7`.
- Genre: Mature dark fantasy combining sword-and-sorcery adventure, horror, and Diablo tie-in fiction.
- Audience: Mature Taiwan readers, primarily Diablo and dark-fantasy readers, while keeping the prose accessible to readers unfamiliar with the games.
- Rationale: Confirmed by the PDF title and copyright pages, the narrative content, and external bibliographic and marketing descriptions.

## 2026-07-11 - Diablo terminology baseline

- Decision: Follow current official Blizzard Traditional Chinese terminology for established franchise names, including `崔斯特姆`, `迪亞布羅`, `巴爾`, `衛斯馬屈`, `卡基斯坦`, and `費斯傑利`.
- Decision: Use `諾瑞克‧維茲哈蘭`, `薩敦‧崔斯特`, and `佛斯汀` for the three principal names introduced in the pilot.
- Rationale: Current Blizzard terminology provides the strongest consistency baseline. `諾瑞克` is also attested in marketing copy for the licensed 2002 Taiwan edition, while the remaining unattested novel-specific names use consistent Taiwan-style transliteration.
- Note: The licensed 2002 Taiwan edition was published as *暗黑破壞神：惡魔的血液*. This project is producing a new translation and will not copy that edition's prose.

## 2026-07-11 - Pilot approval and production baseline

- Decision: Treat the voice, register, punctuation, and character-name forms used in `chunk-0001` as the approved production baseline for the rest of the book.
- Decision: Render `Bartuc` as `霸圖克` and `Horazon` as `赫拉森`, following current official Blizzard Traditional Chinese terminology.
- Decision: Render the epithet `Warlord of Blood` as `鮮血魔將` rather than the political-sounding literal form `鮮血軍閥`.
- Rationale: The user approved the pilot. `鮮血魔將` preserves the martial and demonic force of the title while aligning with current official descriptions of 霸圖克 as a `費斯傑利魔將`.

## 2026-07-11 - Chapter Two names

- Decision: Render `Aranoch` as `亞拉挪奇`, matching the official Traditional Chinese form attested in Diablo III localization data.
- Decision: Render the novel-specific names `Augustus Malevolyn` as `奧古斯都‧馬勒沃林` and `Galeona` as `蓋莉歐娜`.
- Rationale: The character forms are readable Taiwan-style transliterations and remain distinct from established franchise names.
- Decision: Render the novel-specific demon `Xazax` as `薩薩克斯` and `Twin Seas` as `雙子海`.

## 2026-07-12 - Chapter Three names and incantations

- Decision: Follow official Blizzard Traditional Chinese terminology for `Rathma` (`拉斯瑪`) and `necromancer` (`死靈法師`).
- Decision: Render `Kara Nightshadow` as `卡菈‧夜影`; use `卡菈` before the surname appears in the source.
- Decision: Phonetically render the novel-specific demonic commands in Chinese and record each form in the glossary.
- Decision: Render `Scosglen` as `斯科斯格倫`, following current official Blizzard Traditional Chinese terminology.

## 2026-07-12 - Chapter Four terminology

- Decision: Render `Sand Maggot` as `沙蟲` and `Lut Gholein` as `魯高因`, following established/current Blizzard Traditional Chinese terminology.
- Decision: Phonetically render and record the demonic commands and ritual phrases introduced in `chunk-0012`.

## 2026-07-12 - Chapter Five maritime names

- Decision: Use `列王港` for `Kingsport`; no current official Taiwan form was found, and this semantic form is attested in Traditional Chinese Diablo lore references.
- Decision: Render the ships as `納波利斯號`, `鷹火號`, and `奧德賽號`, consistently including the vessel suffix.

## 2026-07-12 - Parallel production and Chapter Nine terminology

- Decision: Increase throughput through parallel candidate translation while retaining sequential primary-agent bilingual review, terminology consolidation, hash recording, and QA before any chunk becomes canonical.
- Rationale: Drafting independent chunks can proceed concurrently, but a single finalization path is required to preserve voice, chronology, and cross-chapter continuity at the approved quality level.
- Decision: Render `Viz-jun` as `威茲君`, following the Traditional Chinese form in an official Diablo III town introduction published through Blizzard's Taiwan site and preserved by Bahamut.

## 2026-07-12 - Final production audit

- Decision: Standardize all numbered chapter headings as H1 Markdown (`# 一` through `# 二十`) and the epilogue as `# 尾聲`.
- Decision: Use Taiwan Traditional Chinese classifier glyph `隻` where the original draft used classifier `只`; retain lexical forms such as `只是`, `只要`, and `只有`.
- Decision: Standardize actual English length units as `呎／吋`, while retaining semantic words and idioms such as `尺寸`, `分寸`, `近在咫尺`, and `寸步難行`.
- Decision: Extend source extraction through PDF page 248 and stop at `About the Author`, preserving the complete epilogue without importing the author biography.
- Result: All 68 chunks passed sequential bilingual review, hash verification, full QA, tests, integrity checks, and complete assembly.
