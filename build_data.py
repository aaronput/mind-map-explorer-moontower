"""
Build the mind-map dataset for Moontower (Kris Abdelmessih).

Sources:
  - https://moontowermeta.com/ (WordPress/Jetpack sitemap index) — the permanent
    archive: essays, notes, wiki pages, and weekly letters (/moontower-N/).
  - https://moontower.substack.com/ — the live Substack; newest posts appear
    here first. Merged in and deduped against moontowermeta by normalized title.

Output: data.json containing threads, concepts, clusters, edges, learning_paths,
quizzes — same shape consumed by index.html (modeled on the Chartbook build).

Strategy:
  - Each content URL becomes a "thread" node (source-tagged: meta | substack).
  - Real titles are scraped from og:title (cached to title_cache.json — first
    run is slow, ~45 min for ~1750 pages, incremental thereafter).
  - Concepts are extracted from real titles + slugs, mapped through an
    options/vol/trading/decision-making taxonomy tuned for Kris's beat.
  - Clusters are broad color groups for the graph.
  - Edges are weighted by Jaccard similarity over (concepts ∪ cluster).
  - Learning paths are curated reading sequences across Moontower's themes.
  - Quizzes auto-generated, one per cluster sample.
"""

import json, os, re, time, html as html_lib
import xml.etree.ElementTree as ET
import urllib.request, urllib.error
from collections import defaultdict, Counter
from itertools import combinations
from pathlib import Path

SKIP_SCRAPE = os.environ.get("SKIP_SCRAPE") == "1"

META_SITEMAP_INDEX = "https://moontowermeta.com/sitemap.xml"
SUBSTACK_SITEMAP   = "https://moontower.substack.com/sitemap.xml"
OUT     = Path(__file__).with_name("data.json")
TITLE_CACHE_FILE = Path(__file__).with_name("title_cache.json")
SCRAPE_DELAY_META     = 1.0   # Kris's own WordPress — gentle but quicker
SCRAPE_DELAY_SUBSTACK = 1.8   # match the Chartbook build's Substack pacing
USER_AGENT   = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 MindMapBuilder/1.0"

NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

# ---------------------------------------------------------------------------
# 1. Pull both sitemaps fresh every run (cheap; refresh.sh relies on this)
# ---------------------------------------------------------------------------
# moontowermeta.com is a Jetpack sitemap *index* → resolve child page sitemaps.
print(f"Fetching sitemap index from {META_SITEMAP_INDEX}...")
idx = ET.fromstring(http_get(META_SITEMAP_INDEX))
page_sitemaps = [
    sm.find("s:loc", NS).text
    for sm in idx.findall("s:sitemap", NS)
    if re.search(r"/sitemap-index-\d+\.xml$", sm.find("s:loc", NS).text)
]
meta_urls = []
for child in page_sitemaps:
    sub = ET.fromstring(http_get(child))
    for sm in sub.findall("s:sitemap", NS):
        loc = sm.find("s:loc", NS).text
        if not re.search(r"/sitemap-\d+\.xml$", loc):
            continue
        leaf = ET.fromstring(http_get(loc))
        for u in leaf.findall("s:url", NS):
            lm = u.find("s:lastmod", NS)
            meta_urls.append({
                "url": u.find("s:loc", NS).text,
                "date": (lm.text[:10] if lm is not None and lm.text else ""),
            })
print(f"moontowermeta.com sitemap URLs: {len(meta_urls)}")

print(f"Fetching Substack sitemap from {SUBSTACK_SITEMAP}...")
ss = ET.fromstring(http_get(SUBSTACK_SITEMAP))
substack_urls = []
for u in ss.findall("s:url", NS):
    loc = u.find("s:loc", NS).text
    if "/p/" not in loc:
        continue
    lm = u.find("s:lastmod", NS)
    substack_urls.append({
        "url": loc,
        "date": (lm.text[:10] if lm is not None and lm.text else ""),
    })
print(f"moontower.substack.com posts: {len(substack_urls)}")

# ---------------------------------------------------------------------------
# 2. Filter moontowermeta to content pages; assemble the raw post list
# ---------------------------------------------------------------------------
# Section indexes, commerce, and account plumbing — not content.
DENY_EXACT = {
    "", "about", "archive", "blog", "books", "cart", "checkout", "contact",
    "essays", "explorables", "guides", "home", "mental", "money", "motivation",
    "my-account", "my-essays", "newsletter", "newsletter-archive", "notes",
    "podcasts", "privacy-policy", "resources", "shop", "start-here",
}
DENY_PREFIX = ("curated-sources", "mental/", "money/", "motivation/",
               "category/", "tag/", "author/", "product")

def keep_meta_path(path):
    p = path.strip("/")
    if p in DENY_EXACT:
        return False
    if any(p == d.rstrip("/") or p.startswith(d) for d in DENY_PREFIX):
        return False
    # weekly letters filed under newsletter-archive/ are content
    if p.startswith("newsletter-archive/") and not re.search(r"moontower-?\d+$", p):
        return False
    return True

posts = []
seen_slugs = set()
for m in meta_urls:
    path = m["url"].split("moontowermeta.com/", 1)[-1].strip("/")
    if not keep_meta_path(path):
        continue
    slug = path.rsplit("/", 1)[-1]
    if slug in seen_slugs:
        continue
    seen_slugs.add(slug)
    posts.append({"slug": slug, "url": m["url"], "date": m["date"], "source": "meta"})

for m in substack_urls:
    slug = m["url"].rsplit("/p/", 1)[-1]
    if slug in seen_slugs:      # same slug on both sites → keep the meta copy
        continue
    seen_slugs.add(slug)
    posts.append({"slug": slug, "url": m["url"], "date": m["date"], "source": "substack"})

posts.sort(key=lambda p: p["date"], reverse=True)
print(f"Candidate posts after slug dedup: {len(posts)} "
      f"(meta {sum(p['source']=='meta' for p in posts)}, "
      f"substack {sum(p['source']=='substack' for p in posts)})")

# ---------------------------------------------------------------------------
# 2b. Real title/subtitle scraper (cached)
# ---------------------------------------------------------------------------
def load_title_cache():
    if TITLE_CACHE_FILE.exists():
        try:
            return json.loads(TITLE_CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_title_cache(cache):
    TITLE_CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))

META_RE = {
    "og:title":       re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    "og:description": re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    "tw:title":       re.compile(r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    "tw:description": re.compile(r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    "title_tag":      re.compile(r'<title[^>]*>([^<]+)</title>', re.I),
}

def _decode(s):
    return html_lib.unescape(s).strip() if s else ""

def fetch_meta(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(200_000).decode("utf-8", errors="replace")
    title    = (META_RE["og:title"].search(body) or META_RE["tw:title"].search(body) or META_RE["title_tag"].search(body))
    subtitle = (META_RE["og:description"].search(body) or META_RE["tw:description"].search(body))
    t = _decode(title.group(1)) if title else ""
    s = _decode(subtitle.group(1)) if subtitle else ""
    # Strip trailing publication suffixes both platforms append.
    t = re.sub(r'\s*[-|–—·]\s*(Party at the Moontower|Moontower(meta)?|Moontower Meta|Kris Abdelmessih)\s*$', '', t, flags=re.I).strip()
    return {"title": t, "subtitle": s}

cache = load_title_cache()
to_fetch = [] if SKIP_SCRAPE else [p for p in posts if not (cache.get(p["slug"]) or {}).get("title")]
if SKIP_SCRAPE:
    print("SKIP_SCRAPE=1 — using slug-derived titles only (no fetches).")
if to_fetch:
    print(f"Scraping {len(to_fetch)} new posts (cache has {len(cache)} entries)...")
    for i, p in enumerate(to_fetch, 1):
        try:
            meta = fetch_meta(p["url"])
            cache[p["slug"]] = {**meta, "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if i % 25 == 0 or i == len(to_fetch):
                print(f"  [{i}/{len(to_fetch)}] {p['slug'][:50]} → {meta['title'][:60]}", flush=True)
                save_title_cache(cache)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            print(f"  [{i}/{len(to_fetch)}] ✗ {p['slug']}: {e}", flush=True)
            cache[p["slug"]] = {"title":"", "subtitle":"", "error":str(e), "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        time.sleep(SCRAPE_DELAY_META if p["source"] == "meta" else SCRAPE_DELAY_SUBSTACK)
    save_title_cache(cache)
    print(f"Title cache now has {len(cache)} entries.")
else:
    print("Title cache is up to date.")

# ---------------------------------------------------------------------------
# 2c. Cross-source dedup by normalized title (substack cross-posts lose)
# ---------------------------------------------------------------------------
def norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", t.lower())

meta_titles = {
    norm_title((cache.get(p["slug"]) or {}).get("title") or "")
    for p in posts if p["source"] == "meta"
}
meta_titles.discard("")
before = len(posts)
posts = [
    p for p in posts
    if not (p["source"] == "substack"
            and norm_title((cache.get(p["slug"]) or {}).get("title") or "") in meta_titles)
]
print(f"Title dedup dropped {before - len(posts)} substack cross-posts → {len(posts)} posts")

# ---------------------------------------------------------------------------
# 3. Slug → title fallback (when scraper hasn't filled in yet)
# ---------------------------------------------------------------------------
ACRONYMS = {
  "ai","ml","gpt","llm","llms","api","etf","etfs","vix","spx","spy","qqq","ndx",
  "iv","rv","vrp","atm","otm","itm","ev","evs","pnl","p-l","bsm","mm","hft",
  "btc","eth","defi","nft","nfts","dcf","cagr","irr","roi","fire","hsa","401k",
  "sig","cme","cboe","nyse","sec","fomc","fed","gdp","cpi","reit","reits",
  "dfw","nba","nfl","mlb","ercot","usa","us","uk","la","nyc","sf","tx","ca",
}

TITLE_FIXES = {"vs": "vs", "faq": "FAQ", "qa": "Q&A"}

def slug_to_title(slug):
    words = slug.split("-")
    out = []
    for w in words:
        wl = w.lower()
        if wl in ACRONYMS:
            out.append(wl.upper())
        elif wl in TITLE_FIXES:
            out.append(TITLE_FIXES[wl])
        elif w.isdigit() or re.match(r"^\d+[a-z]+$", wl):
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)

# ---------------------------------------------------------------------------
# 4. Concept taxonomy & extraction — tuned for Kris's beat
# ---------------------------------------------------------------------------
TAXONOMY = {
  # Options & volatility
  "Options Basics":   ["option","options","call","calls","put","puts","strike","strikes","moneyness","expiry","expiration","exercise"],
  "Implied Vol":      ["implied-vol","implied-volatility","iv","vol-surface","surface"],
  "Volatility":       ["vol","volatility","vols","realized-vol","realized-volatility"],
  "Skew & Smile":     ["skew","smile","risk-reversal","tails","kurtosis","convexity"],
  "Term Structure":   ["term-structure","calendar","calendars","contango","backwardation"],
  "Greeks":           ["greek","greeks","delta","gamma","vega","theta","vanna","volga","charm","shadow-theta"],
  "Hedging":          ["hedge","hedges","hedging","hedge-ratios","replication","replicating"],
  "Vol Risk Premium": ["vrp","variance-risk-premium","vol-premium","overwriting","covered-call","covered-calls"],
  "Option Structures":["straddle","straddles","strangle","strangles","butterfly","iron-butterfly","condor","collar","vertical","spreads"],
  "Dispersion & Correlation":["dispersion","correlation","correlations","index-vol"],
  "VIX":              ["vix","vixation"],
  "Variance & Vol Math":["variance","volatility-drain","volatility-tax","geometric","arithmetic","compounding","lognormal","rebalancing-premium"],
  "Earnings & Events":["earnings","event","events","catalyst","fomc"],
  "Moontower.ai":     ["moontower-ai","vol-dashboard","volatility-lab"],

  # Trading & market making
  "Market Making":    ["market-maker","market-makers","market-making","liquidity","bid-ask","order-flow","adverse-selection","quotes","quoting"],
  "Edge & EV":        ["edge","expected-value","expectancy","alpha","mispricing","cheap","expensive","fair-value"],
  "Kelly & Sizing":   ["kelly","bet-sizing","bet-size","position-sizing","sizing","bankroll","ruin","drawdown"],
  "Execution":        ["execution","slippage","microstructure","spread-crossing","transaction-costs"],
  "Trading Careers":  ["sig","susquehanna","prop-trading","prop-trader","trading-career","trader","traders","pit","floor","desk","clerk"],
  "Mock Trading & Games":["mock-trading","trading-game","market-game","trading-games","simulation"],
  "Risk Management":  ["risk","risks","tail-risk","blowup","blow-up","leverage","levered","margin"],
  "Arbitrage & Parity":["arbitrage","arb","parity","put-call-parity","box","boxes","carry"],

  # Probability & betting
  "Probability":      ["probability","probabilities","odds","bayes","bayesian","monte-carlo","distribution","distributions","randomness","luck","variance-luck"],
  "Poker":            ["poker","holdem","hold-em","wsop","bluff"],
  "Gambling & Betting":["betting","bet","bets","gamble","gambling","gambler","casino","blackjack","sportsball","sports-betting","parlay","wager","lottery","horse"],
  "Puzzles & Teasers":["puzzle","puzzles","teaser","teasers","riddle","brain-teaser","interview-question"],

  # Investing & markets
  "Indexing & Passive":["index","indexing","passive","etf","etfs","vanguard","bogle"],
  "Portfolio & Diversification":["diversification","diversified","portfolio","portfolios","rebalancing","asset-allocation","allocation","barbell"],
  "Bonds & Rates":    ["bond","bonds","rates","interest-rates","treasury","treasuries","yield","yields","duration","inflation"],
  "Real Estate":      ["real-estate","housing","house","mortgage","rent-vs-buy","rent","landlord","home-ownership","homeowner"],
  "Angel & Startups": ["angel","startup","startups","venture","vc","seed"],
  "Crypto":           ["crypto","bitcoin","btc","ethereum","eth","defi","token","tokens","nft","nfts","stablecoin"],
  "Commodities & Energy":["oil","gas","energy","ercot","uranium","gold","silver","commodity","commodities","power","electricity"],
  "Equities":         ["stock","stocks","equity","equities","spx","spy","tesla","gamestop","meme-stock","shares"],
  "Returns & Valuation":["returns","return","cagr","valuation","expected-return","expected-returns","premium","yield-curve"],

  # Money & life
  "Personal Finance": ["money","savings","saving","budget","taxes","tax","401k","hsa","fire","retirement","insurance","spending"],
  "Wealth & Status":  ["wealth","wealthy","rich","enough","status","envy","jealousy","keeping-up","affluence"],

  # Learning & education
  "School & Education":["school","schools","education","homework","teacher","teachers","classroom","curriculum","college","tuition","game-of-school"],
  "Math & Numeracy":  ["math","maths","arithmetic","algebra","calculus","numeracy","innumeracy","fractions","geometry"],
  "Learning":         ["learning","learn","pedagogy","practice","deliberate-practice","tutoring","explorables","spaced-repetition"],

  # Career & work
  "Career":           ["career","careers","job","jobs","resume","interview","interviews","quit","quitting","promotion","salary","slashie"],
  "Entrepreneurship": ["entrepreneur","entrepreneurship","business","businesses","founder","saas","bootstrapping","solopreneur"],
  "Productivity":     ["productivity","habits","habit","systems","workflow","tools","notion","obsidian"],

  # Mind & decisions
  "Decision Making":  ["decision","decisions","decide","choices","choice","tradeoff","tradeoffs","optionality","reversible"],
  "Mental Models":    ["mental-model","mental-models","models","framework","frameworks","munger","first-principles","inversion"],
  "Psychology & Incentives":["psychology","psychological","bias","biases","behavioral","incentive","incentives","signaling","persuasion","fomo"],
  "Well-Being":       ["happiness","happy","gratitude","serenity","meditation","anxiety","therapy","mindfulness","boredom","contentment","thrive"],

  # Family & living
  "Parenting & Family":["parenting","parent","parents","kids","kid","children","son","sons","father","mother","family","cousin-camp","dad","mom"],
  "Travel & Places":  ["travel","trip","mendocino","yosemite","vegas","hawaii","road-trip","vacation"],
  "Music & Culture":  ["music","song","songs","album","movie","movies","film","concert","evh","van-halen","guitar","hip-hop","playlist"],

  # Notes & media
  "Book Notes":       ["book","books","reading","read","notes-from","excerpts","excerpt"],
  "Podcast Notes":    ["podcast","podcasts","invest-like-the-best","episode","interview-notes"],
  "Writing & Blogging":["writing","write","writers","blog","blogging","newsletter","newsletters","substack","audience","publish","learning-in-public"],

  # Tech & AI
  "AI & Tech":        ["ai","gpt","gpt-stuff","llm","llms","chatgpt","machine-learning","software","coding","code","programming","tech","technology","internet","ai-traders"],

  # Series
  "Moontower Letter": ["moontower"],
}

CLUSTERS = [
  ("Options & Volatility",     {"Options Basics","Implied Vol","Volatility","Skew & Smile","Term Structure","Greeks","Hedging","Vol Risk Premium","Option Structures","Dispersion & Correlation","VIX","Variance & Vol Math","Earnings & Events","Moontower.ai"}),
  ("Trading & Market Making",  {"Market Making","Edge & EV","Kelly & Sizing","Execution","Trading Careers","Mock Trading & Games","Risk Management","Arbitrage & Parity"}),
  ("Probability & Betting",    {"Probability","Poker","Gambling & Betting","Puzzles & Teasers"}),
  ("Investing & Markets",      {"Indexing & Passive","Portfolio & Diversification","Bonds & Rates","Real Estate","Angel & Startups","Crypto","Commodities & Energy","Equities","Returns & Valuation"}),
  ("Money & Life",             {"Personal Finance","Wealth & Status"}),
  ("Learning & Education",     {"School & Education","Math & Numeracy","Learning"}),
  ("Career & Work",            {"Career","Entrepreneurship","Productivity"}),
  ("Mind & Decisions",         {"Decision Making","Mental Models","Psychology & Incentives","Well-Being"}),
  ("Family & Living",          {"Parenting & Family","Travel & Places","Music & Culture"}),
  ("Notes & Media",            {"Book Notes","Podcast Notes","Writing & Blogging"}),
  ("Tech & AI",                {"AI & Tech"}),
  ("Moontower Letters",        {"Moontower Letter"}),
]

CLUSTER_COLORS = {
  "Options & Volatility":     "#edc948",
  "Trading & Market Making":  "#f28e2b",
  "Probability & Betting":    "#e15759",
  "Investing & Markets":      "#4e79a7",
  "Money & Life":             "#59a14f",
  "Learning & Education":     "#76b7b2",
  "Career & Work":            "#ff9da7",
  "Mind & Decisions":         "#b07aa1",
  "Family & Living":          "#9c755f",
  "Notes & Media":            "#bab0ac",
  "Tech & AI":                "#af7aa1",
  "Moontower Letters":        "#6b8fc9",
  "Other":                    "#6e7681",
}

def extract_concepts(text):
    s = re.sub(r"[^a-z0-9\-]+", "-", text.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    hits = []
    for concept, patterns in TAXONOMY.items():
        for p in patterns:
            if "-" in p:
                if p in s:
                    hits.append(concept); break
            else:
                if re.search(rf"(^|-){re.escape(p)}($|-)", s):
                    hits.append(concept); break
    return list(dict.fromkeys(hits))

def assign_cluster(concepts, post_type):
    if post_type == "letter":
        return "Moontower Letters"
    scores = []
    for name, members in CLUSTERS:
        if name == "Moontower Letters":
            continue
        scores.append((name, sum(1 for c in concepts if c in members)))
    scores.sort(key=lambda x: -x[1])
    return scores[0][0] if scores[0][1] > 0 else "Other"

def infer_post_type(slug):
    """letter = the weekly Moontower letter (moontower-N); essay = everything else."""
    s = slug.lower()
    if re.match(r"^moontower-?\d+$", s):
        return "letter"
    return "essay"

def infer_difficulty(concepts, slug):
    quanty = {"Greeks","Implied Vol","Skew & Smile","Vol Risk Premium","Dispersion & Correlation",
              "Variance & Vol Math","Hedging","Term Structure","Arbitrage & Parity","Market Making","Execution"}
    if any(c in quanty for c in concepts): return "advanced"
    trading = {"Options Basics","Volatility","VIX","Option Structures","Kelly & Sizing","Edge & EV",
               "Probability","Risk Management","Indexing & Passive","Portfolio & Diversification",
               "Bonds & Rates","Returns & Valuation","Crypto","Equities","Commodities & Energy"}
    if any(c in trading for c in concepts): return "intermediate"
    return "beginner"

# ---------------------------------------------------------------------------
# 5. Build thread nodes
# ---------------------------------------------------------------------------
threads = []
concept_set = Counter()
for idx, p in enumerate(posts):
    slug = p["slug"]
    cached = cache.get(slug) or {}
    real_title    = cached.get("title")    or ""
    real_subtitle = cached.get("subtitle") or ""
    title   = real_title or slug_to_title(slug)
    src_lbl = "Moontower" if p["source"] == "meta" else "Substack"
    summary = real_subtitle or f"{src_lbl} · {p['date']}"

    post_type = infer_post_type(slug)
    concepts = extract_concepts(slug + " " + real_title.lower() + " " + real_subtitle.lower())
    cluster  = assign_cluster(concepts, post_type)
    diff     = infer_difficulty(concepts, slug)

    threads.append({
        "id": f"t{idx}",
        "slug": slug,
        "title": title,
        "url": p["url"],
        "date": p["date"],
        "summary": summary,
        "concepts": concepts,
        "cluster": cluster,
        "difficulty": diff,
        "post_type": post_type,
        "source": p["source"],
    })
    for c in concepts: concept_set[c] += 1

# ---------------------------------------------------------------------------
# 6. Concept nodes
# ---------------------------------------------------------------------------
concepts_out = []
for name, count in concept_set.most_common():
    concepts_out.append({
        "id": f"c_{re.sub(r'[^a-z0-9]+','_', name.lower())}",
        "name": name,
        "count": count,
    })

# ---------------------------------------------------------------------------
# 7. Edges between threads (Jaccard, with degree cap)
# ---------------------------------------------------------------------------
edges = []
by_id = {t["id"]: set(t["concepts"]) | {"@cluster:" + t["cluster"]} for t in threads}
ids = list(by_id.keys())
EDGE_THRESHOLD = 0.22
MAX_DEG = 6
adj = defaultdict(list)
for a, b in combinations(ids, 2):
    sa, sb = by_id[a], by_id[b]
    if not sa or not sb: continue
    inter = len(sa & sb)
    if inter < 2: continue
    union = len(sa | sb)
    j = inter / union
    if j >= EDGE_THRESHOLD:
        adj[a].append((b, j, inter))
        adj[b].append((a, j, inter))

seen = set()
for a, neighbors in adj.items():
    neighbors.sort(key=lambda x: -x[1])
    for b, j, inter in neighbors[:MAX_DEG]:
        key = tuple(sorted([a,b]))
        if key in seen: continue
        seen.add(key)
        edges.append({"source": a, "target": b, "weight": round(j,3), "shared": inter, "type":"thread"})

print(f"Edges: {len(edges)}; threads: {len(threads)}; concepts: {len(concepts_out)}")

# ---------------------------------------------------------------------------
# 8. Learning paths
# ---------------------------------------------------------------------------
def path_for(name, *needles, limit=12):
    matches = []
    for t in threads:
        blob = (t["slug"] + " " + t["title"]).lower()
        if any(n in blob for n in needles):
            matches.append(t["id"])
    return {"name": name, "threads": matches[:limit]}

learning_paths = [
    path_for("Options Starter Pack",
             "options-starter","option-theory","what-is-an-option","options-101","intro-to-options","option-basics"),
    path_for("Understanding Implied Vol",
             "implied-vol","implied-volatility","vol-surface","what-is-vol","volatility-term"),
    path_for("Greeks & Hedging",
             "delta","gamma","vega","theta","greek","hedge","hedging","shadow-theta"),
    path_for("Vol Risk Premium & Selling Options",
             "vrp","variance-risk","covered-call","overwriting","selling-options","short-vol","premium"),
    path_for("Structures: Straddles to Butterflies",
             "straddle","strangle","butterfly","condor","collar","vertical","spread"),
    path_for("Market Making & Edge",
             "market-mak","edge","adverse-selection","order-flow","liquidity","bid-ask","fair-value"),
    path_for("Kelly, Sizing & Risk of Ruin",
             "kelly","sizing","bankroll","ruin","bet-siz","drawdown"),
    path_for("Poker, Gambling & Probability",
             "poker","gambl","betting","casino","blackjack","odds","probability"),
    path_for("Volatility Math & the Rebalancing Premium",
             "variance","volatility-drain","volatility-tax","geometric","arithmetic","compounding","rebalancing"),
    path_for("Rent vs Buy & Real Estate",
             "rent-vs-buy","rent","mortgage","housing","real-estate","home"),
    path_for("The Game of School & Learning Math",
             "game-of-school","school","math","numeracy","innumeracy","homework","education","learning"),
    path_for("Trading as a Career",
             "trading-career","trader","sig","susquehanna","prop","pit","clerk","career"),
    path_for("Wealth, Status & Enough",
             "wealth","rich","enough","status","envy","jealousy","money"),
    path_for("Writing & Learning in Public",
             "writing","blog","newsletter","substack","audience","learning-in-public"),
]
learning_paths = [p for p in learning_paths if len(p["threads"]) >= 3]

# ---------------------------------------------------------------------------
# 9. Quizzes
# ---------------------------------------------------------------------------
quizzes = []
by_cluster = defaultdict(list)
for t in threads: by_cluster[t["cluster"]].append(t)
for cl, ts in by_cluster.items():
    for t in ts[:3]:
        if not t["concepts"]: continue
        quizzes.append({
            "id": f"q_{t['id']}",
            "thread": t["id"],
            "question": f"Which concepts does \"{t['title']}\" primarily touch?",
            "answer": ", ".join(t["concepts"][:4]) or "—",
            "cluster": cl,
        })

# ---------------------------------------------------------------------------
# 10. Emit
# ---------------------------------------------------------------------------
data = {
  "publication": {
    "name": "Moontower",
    "tagline": "Kris Abdelmessih · options, vol & clear thinking",
    "url": "https://moontowermeta.com/",
    "post_count": len(threads),
  },
  "clusters": [{"name": name, "color": CLUSTER_COLORS.get(name,"#6e7681")} for name,_ in CLUSTERS] + [{"name":"Other","color":CLUSTER_COLORS["Other"]}],
  "threads": threads,
  "concepts": concepts_out,
  "edges": edges,
  "learning_paths": learning_paths,
  "quizzes": quizzes,
}

OUT.write_text(json.dumps(data, indent=2))
print(f"Wrote {OUT} ({OUT.stat().st_size:,} bytes)")
