#!/usr/bin/env bash
# Refresh the Moontower mind map from live sources (moontowermeta.com +
# moontower.substack.com), re-inline into index.html, and push to GitHub.
# Vercel auto-deploys ~30s after the push.
set -euo pipefail

# Cron runs with a stripped PATH. Re-add the locations our tools live in.
export PATH="/opt/homebrew/bin:/opt/anaconda3/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
echo "→ [$(date '+%Y-%m-%d %H:%M:%S')] Working in: $(pwd)"

# 1. Rebuild the enriched dataset (build_data.py fetches its own sitemaps)
echo "→ Rebuilding data.json..."
python3 build_data.py

# 2. Re-inline data.json into index.html
echo "→ Re-inlining dataset into index.html..."
python3 - <<'PY'
import re, pathlib
html = pathlib.Path("index.html").read_text()
data = pathlib.Path("data.json").read_text().replace("</", "<\\/")
new_html, n = re.subn(
    r'(<script id="dataset"[^>]*>).*?(</script>)',
    lambda m: m.group(1) + data + m.group(2),
    html, count=1, flags=re.S,
)
if n != 1:
    raise SystemExit("ERROR: could not find <script id=\"dataset\"> block in index.html")
pathlib.Path("index.html").write_text(new_html)
print(f"   index.html now {len(new_html):,} chars")
PY

# 3. Stage, commit, push — but only if something actually changed
git add data.json index.html title_cache.json 2>/dev/null || true
if git diff --cached --quiet; then
  echo "→ No changes — nothing to deploy."
  exit 0
fi

STAMP="$(date +%Y-%m-%d)"
git commit -m "Refresh dataset ${STAMP}"
git push
echo "✓ Pushed. Vercel will redeploy in ~30s."
