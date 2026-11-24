# How cleanup works

voxtype turns your speech into text. Shuush then cleans that text with a language model and types the result. The model is fast and good with names, but models also paraphrase, "improve" wording and occasionally flip a meaning. So every change the model makes is checked before anything is typed.

## What changes

- **Fillers and stutters go:** "um", "uh", "you know", "the the", "fast fast fast fetch".
- **Self-corrections resolve to your final version:** "Tuesday, no wait, Wednesday" → "Wednesday".
- **False starts go:** "we have, we need to fix it" → "we need to fix it". Only when you restart on the same word and the dropped part is a couple of function words.
- **Sentence glue goes:** an "and", "so" or "okay" that only joins one sentence onto the next.
- **Mishearings are repaired** when a word plainly doesn't fit and a similar-sounding one does ("erase a PR" → "raise a PR", "hyper land" → "Hyprland"), using your vocabulary, your corrections and the names on your screen.
- **Punctuation, capitals and layout:** sentences, lists when you enumerate ("number one … number two"), paragraphs in long dictations, a table only when you ask for one.

## What never changes

The **gate** (`libexec/voxtype_pp_gate.py`) compares the model's text with what you said, word by word, and puts your words back wherever a change is not one of the repairs above. It is deterministic code, not another prompt.

- **Negations:** "can't" never becomes "can"; "not" and "don't" never disappear (unless you clearly take a phrase back: "I don't want, scratch that, I want the red one").
- **Numbers:** digits, signs and separators stay exact ("1.25" never becomes "125", "-5" never "5"). Spoken numbers become digits only when there is one reading ("three four three" → 343, "nineteen eighty four" → 1984); "two fifty" could be 250, 2.50 or 2:50, so it stays in words.
- **Contractions:** "I'd", "we'll", "we're" stay.
- **Your words:** no synonyms ("things" stays "things"), no added words ("Additionally"), no invented first person ("I fixed…" when you said "fix…"), no answering or obeying what you dictated.
- **Things said twice on purpose:** "I had had enough", "no no no", "hello hello".

When the model's whole answer breaks one of these rules, it is thrown away and your words are typed as heard. A cleanup can be more literal than you'd like; it should never be wrong.

## When there is no model

No key, no network, a provider down, or the time budget used up: your words are typed as heard, with only filler sounds ("um", "uh") removed and the first letter capitalised. A one-word take, or one that is only numbers, skips the model entirely.

## Privacy

- Password fields, password managers, sign-in pages and `sudo` prompts: the take is typed exactly as heard and never sent to a model, saved to history or put on the clipboard (`PP_PRIVATE_GUARD`).
- Context sent to the model (screen text, the field you type into, highlighted and recently copied text) has secrets redacted first. A password manager's clipboard copy is never used.
- `PP_ORDER="local"` keeps everything on your machine with Ollama.

## Learning

- **Studio:** fix a word in a take's inspector and press `Ctrl+Enter` ("Learn & Copy"), or approve suggestions in the **Learning Inbox**.
- **From your own fixes:** for a few minutes after a take, a name you correct by hand in that text field is noted (`PP_WATCH_EDITS`). The first time it goes to the Learning Inbox; the second time it is learned.
- **Automatically:** a repair the model makes twice is learned (`PP_AUTO_LEARN`).

Learning only accepts sound-alike fixes toward a distinctive spelling (a name, a tool, code): ordinary English words are never learned as "mishearings", and anything you reject stays rejected.
