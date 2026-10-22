# README Presentation — Recipes

Depth for `portfolio-repo-audit` steps 4 and 5, plus post-push verification.

---

## 1. Screenshot prep

### Crop to the best region instead of shipping a visual defect

Screenshots of long pages usually contain something you do not want to show
(unrendered markup, a truncated table, an error toast) below the good part. Do
not eyeball the crop line — find a blank horizontal band programmatically.

```python
from PIL import Image

im = Image.open(src).convert("RGB")
W, H = im.size
px = im.load()

# sample the page background from a known-empty spot in the content column
bg = px[1800, 950]

def row_is_blank(y, x0=600, x1=1940, tol=6, step=3):
    for x in range(x0, x1, step):
        r, g, b = px[x, y]
        if abs(r - bg[0]) > tol or abs(g - bg[1]) > tol or abs(b - bg[2]) > tol:
            return False
    return True

# collect runs of blank rows, then cut inside the run that separates
# the region you want from the region you don't
bands, start = [], None
for y in range(850, 1351):
    if row_is_blank(y):
        start = y if start is None else start
    else:
        if start is not None and y - start >= 15:
            bands.append((start, y - 1))
        start = None

print(bands)      # inspect, then choose
cut = 1170        # middle of the chosen band
im.crop((0, 0, W, cut)).save(out, "PNG")
```

**Verify the crop visually before committing it.** A cut a few pixels too low lands
mid-sentence and looks broken; check that the last visible line is a complete
logical block. A trailing ellipsis in body text may be the application's own
summary truncation rather than your crop — compare against the uncropped original
before "fixing" it.

### Cut file size with palette quantization

UI screenshots have a limited colour count, so quantizing to a 256-colour palette
typically removes 55–65% of the bytes with no visible text degradation:

```python
from PIL import Image
im = Image.open(name).convert("RGB")
im.quantize(colors=256, method=Image.MEDIANCUT, dither=Image.NONE) \
  .save(name, "PNG", optimize=True)
```

Downscale to about 1600px wide first — GitHub renders markdown body content at
roughly 880px, so 1600px is a comfortable 2x asset and anything wider is waste. Do
**not** use JPEG for screenshots containing text; the ringing artifacts around
glyphs look worse than the bytes saved.

### README section template

Place it after the one-line project description and before the feature table.

```markdown
## 界面预览 / Screenshots

**Primary mode** — one sentence on what the reader is looking at and which
mechanism it demonstrates:

![primary mode](resources/screenshots/01-primary.png)

**Secondary mode** — the differentiating capability:

![secondary mode](resources/screenshots/02-secondary.png)

> One line on layout conventions worth noticing.
```

Keep images in a tracked directory (`resources/screenshots/`), use ASCII
filenames, and never put them under a gitignored path.

---

## 2. About panel: description and topics

Click path: repo home page → right sidebar → **About** heading → **gear icon**.

**Description** — one line: what it is, then the three or four techniques that make
it non-trivial. Roughly 150–180 characters:

```
Personal RAG knowledge base — parent-child chunking with offset mapping,
cross-encoder reranking, query rewrite, multi-turn chat.
Python · ChromaDB · bge-large-zh · DeepSeek
```

**Topics** — lower-case, hyphenated, no spaces, max 20. Topics drive GitHub search
and related-repo surfacing, so they matter more than most people assume. For a
retrieval / LLM project, a workable set:

```
rag
retrieval-augmented-generation
llm
chromadb
vector-database
reranking
cross-encoder
streamlit
python
```

Press **Save changes**, then verify:

```bash
curl -s https://api.github.com/repos/<owner>/<repo> \
  | python -c "import sys,json; d=json.load(sys.stdin); print(d['description'], d['topics'])"
```

---

## 3. Post-push verification

A raw-content fetch can fail for network reasons and look like "the push did not
land". Verify through the API and compare hashes instead of assuming:

```bash
# remote content of a file (base64) + its blob hash
curl -s "https://api.github.com/repos/<owner>/<repo>/contents/README.md?ref=master" \
  | python -c "import sys,json,base64; d=json.load(sys.stdin); \
print(d['sha']); print(base64.b64decode(d['content']).decode('utf-8','replace')[:400])"

# local hash of the same file — matching shas prove a byte-identical push
git hash-object README.md
git rev-parse master:README.md
```

Other checks worth running after a polish pass:

```bash
# did the images and the license actually land?
curl -s "https://api.github.com/repos/<owner>/<repo>/git/trees/HEAD?recursive=1" \
  | python -c "import sys,json; [print(t.get('size','-'), t['path']) for t in json.load(sys.stdin)['tree']]"

# does GitHub recognise the license?
curl -s https://api.github.com/repos/<owner>/<repo> \
  | python -c "import sys,json; print(json.load(sys.stdin)['license'])"

# is the README reachable on the default branch?
curl -s -o /dev/null -w "%{http_code}\n" https://api.github.com/repos/<owner>/<repo>/readme
```

A freshly recreated repo may report `default_branch: main` while you pushed
`master`; GitHub normally re-points the default to the single existing branch.
Confirm it resolves, or the home page shows the empty-repo setup instructions
instead of the README.

---

## 4. Reviewer's-eye pass, in order

1. Open the home page as a stranger: description, screenshot, license badge, a
   README that starts with what the thing does.
2. Can a clean clone install and run from the README alone? (dependency coverage
   script, plus a config template that exists in the tree)
3. Does every numeric claim have a measurement behind it, and does every figure
   quoted about the repo match it?
4. Does the commit history read as iteration or as one dump?
5. Are there tests?
6. Anything in images, commit messages, or docs the author would not want a
   recruiter to read?
