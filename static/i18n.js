/* CollectIQ 前端多國語言（zh-Hant 原文 → en / ja / ko）
 * 作法：以「原始繁中文字」為 key 翻譯 leaf text node，配合 MutationObserver
 * 涵蓋 JS 動態渲染的內容。資料值（卡名、價格）不在字典內 → 原樣保留。
 * 語言存 localStorage('collectiq_lang')，預設 zh。
 */
(function () {
  "use strict";

  // ---- 字典：key = 繁中原文（trim 後的 leaf text）----
  const DICT = {
    // 導覽列 nav
    "總覽":        { en: "Overview",       ja: "概要",           ko: "개요" },
    "價格驗證":    { en: "Price Verify",   ja: "価格検証",       ko: "가격 검증" },
    "CDP 模擬":    { en: "CDP Sim",        ja: "CDPシミュ",      ko: "CDP 시뮬" },
    "RWA 指數":    { en: "RWA Index",      ja: "RWA指数",        ko: "RWA 지수" },
    "鏈上持有":    { en: "On-chain",       ja: "オンチェーン保有", ko: "온체인 보유" },
    "API 狀態":    { en: "API Status",     ja: "APIステータス",   ko: "API 상태" },
    "外部比價":    { en: "Price Compare",  ja: "外部価格比較",    ko: "외부 시세" },
    "🎰 限量歷史": { en: "🎰 Limited History", ja: "🎰 限定履歴", ko: "🎰 한정 이력" },

    // 頁面標題 / hero heading
    "CDP 質押模擬器":   { en: "CDP Collateral Simulator", ja: "CDP 担保シミュレーター", ko: "CDP 담보 시뮬레이터" },
    "RWA 收藏品指數":   { en: "RWA Collectibles Index",   ja: "RWA コレクション指数",   ko: "RWA 수집품 지수" },
    "限量卡機歷史":     { en: "Limited Pack History",     ja: "限定パック履歴",         ko: "한정 팩 이력" },
    "獨立價格驗證":     { en: "Independent Price Verification", ja: "独立価格検証", ko: "독립 가격 검증" },
    "🔍 PriceCharting 外部價格搜尋": { en: "🔍 PriceCharting Price Search", ja: "🔍 PriceCharting 価格検索", ko: "🔍 PriceCharting 가격 검색" },

    // hero 副標
    "選擇一張卡片 → 即時計算可借款額度、清算價格、LTV":
      { en: "Pick a card → instantly compute borrow limit, liquidation price, LTV",
        ja: "カードを選択 → 借入可能額・清算価格・LTV を即時計算",
        ko: "카드 선택 → 대출 한도·청산 가격·LTV 즉시 계산" },
    "按系列追蹤價格分佈、驗證覆蓋率與信任分數 — 像追蹤股票指數一樣追蹤收藏品":
      { en: "Track price distribution, verification coverage and trust score by set — like a stock index for collectibles",
        ja: "シリーズ別に価格分布・検証カバレッジ・信頼スコアを追跡 — 株価指数のようにコレクションを追う",
        ko: "시리즈별 가격 분포·검증 커버리지·신뢰 점수 추적 — 주가지수처럼 수집품을 추적" },
    "限量卡機開放窗口 · 鏈上獎池即時追蹤":
      { en: "Limited pack open window · on-chain prize pool live tracking",
        ja: "限定パック開放ウィンドウ · オンチェーン賞金プール即時追跡",
        ko: "한정 팩 오픈 기간 · 온체인 상금 풀 실시간 추적" },
    "每次偵測到的限量卡機開放 / 新增 / S 卡事件時間軸":
      { en: "Timeline of every detected limited-pack open / add / S-card event",
        ja: "検出された限定パックの開放 / 追加 / Sカードイベントのタイムライン",
        ko: "감지된 한정 팩 오픈 / 추가 / S카드 이벤트 타임라인" },

    // 區塊標題 section headings
    "📋 選擇質押品":   { en: "📋 Select Collateral", ja: "📋 担保を選択",   ko: "📋 담보 선택" },
    "💰 質押評估":     { en: "💰 Collateral Assessment", ja: "💰 担保評価", ko: "💰 담보 평가" },
    "📊 投資組合質押（多卡同時質押）":
      { en: "📊 Portfolio Collateral (multiple cards)", ja: "📊 ポートフォリオ担保（複数カード）", ko: "📊 포트폴리오 담보 (다중 카드)" },
    "📊 系列總市值排行 Top 10":
      { en: "📊 Top 10 Sets by Market Cap", ja: "📊 シリーズ時価総額 Top 10", ko: "📊 시리즈 시가총액 Top 10" },
    "📊 動態 LTV 分級": { en: "📊 Dynamic LTV Tiers", ja: "📊 動的 LTV 区分", ko: "📊 동적 LTV 등급" },
    "🎲 即時卡機 EV":  { en: "🎲 Live Pack EV", ja: "🎲 リアルタイムEV", ko: "🎲 실시간 팩 EV" },
    "📈 最近跨平台成交": { en: "📈 Recent Cross-platform Sales", ja: "📈 最近のクロス取引", ko: "📈 최근 크로스 거래" },
    "即時抽卡事件流":   { en: "Live Pull Event Stream", ja: "リアルタイム抽選イベント", ko: "실시간 뽑기 이벤트" },

    // 表格欄位 table headers
    "系列名稱":   { en: "Set Name",       ja: "シリーズ名",   ko: "시리즈명" },
    "卡片數":     { en: "Cards",          ja: "枚数",         ko: "카드 수" },
    "已驗證":     { en: "Verified",       ja: "検証済み",     ko: "검증됨" },
    "覆蓋率":     { en: "Coverage",       ja: "カバレッジ",   ko: "커버리지" },
    "平均 FMV":   { en: "Avg FMV",        ja: "平均FMV",      ko: "평균 FMV" },
    "平均驗證價": { en: "Avg Verified",   ja: "平均検証価",   ko: "평균 검증가" },
    "平均偏差":   { en: "Avg Delta",      ja: "平均偏差",     ko: "평균 편차" },
    "信任分數":   { en: "Trust Score",    ja: "信頼スコア",   ko: "신뢰 점수" },
    "總市值":     { en: "Market Cap",     ja: "時価総額",     ko: "시가총액" },
    "驗證價":     { en: "Verified",       ja: "検証価",       ko: "검증가" },
    "清算價":     { en: "Liq. Price",     ja: "清算価",       ko: "청산가" },
    "可質押價值": { en: "Collateral Value", ja: "担保価値",   ko: "담보 가치" },
    "卡片":       { en: "Card",           ja: "カード",       ko: "카드" },
    "等級":       { en: "Grade",          ja: "グレード",     ko: "등급" },
    "掛單價":     { en: "Ask Price",      ja: "出品価",       ko: "호가" },
    "市場":       { en: "Market",         ja: "市場",         ko: "시장" },
    "判定":       { en: "Verdict",        ja: "判定",         ko: "판정" },
    "來源":       { en: "Source",         ja: "ソース",       ko: "출처" },
    "外部驗證價": { en: "Ext. Verified",  ja: "外部検証価",   ko: "외부 검증가" },
    "類型":       { en: "Type",           ja: "タイプ",       ko: "유형" },
    "日期":       { en: "Date",           ja: "日付",         ko: "날짜" },
    "錢包":       { en: "Wallet",         ja: "ウォレット",   ko: "지갑" },
    "持有人":     { en: "Holder",         ja: "保有者",       ko: "보유자" },
    "持有數":     { en: "Qty",            ja: "保有数",       ko: "보유 수" },
    "分級":       { en: "Grade",          ja: "グレード",     ko: "등급" },
    "市場價":     { en: "Market Price",   ja: "市場価",       ko: "시장가" },

    // 篩選 / 排序 / 按鈕
    "全部 IP":         { en: "All IP",         ja: "全 IP",        ko: "전체 IP" },
    "全部信心":        { en: "All Confidence", ja: "全信頼度",     ko: "전체 신뢰도" },
    "全部":            { en: "All",            ja: "すべて",       ko: "전체" },
    "按卡片數排序":    { en: "Sort by Cards",     ja: "枚数順",       ko: "카드 수 순" },
    "按總市值排序":    { en: "Sort by Market Cap", ja: "時価総額順",  ko: "시가총액 순" },
    "按信任分數排序":  { en: "Sort by Trust",     ja: "信頼スコア順", ko: "신뢰 점수 순" },
    "按平均偏差排序":  { en: "Sort by Delta",     ja: "偏差順",       ko: "편차 순" },
    "按覆蓋率排序":    { en: "Sort by Coverage",  ja: "カバレッジ順", ko: "커버리지 순" },
    "只看有圖":        { en: "With image only",   ja: "画像ありのみ", ko: "이미지만" },
    "只看已識別":      { en: "Identified only",   ja: "識別済みのみ", ko: "식별됨만" },
    "▶ 執行全部驗證":  { en: "▶ Verify All",      ja: "▶ 全て検証",   ko: "▶ 전체 검증" },
    "▶ 重新執行":      { en: "▶ Re-run",          ja: "▶ 再実行",     ko: "▶ 다시 실행" },
    "查這張 →":        { en: "Check this →",      ja: "これを確認 →", ko: "이 카드 확인 →" },
    "查看完整驗證表 →":{ en: "View full table →", ja: "全検証表を見る →", ko: "전체 검증표 보기 →" },
    "自動更新 (8s)":   { en: "Auto refresh (8s)", ja: "自動更新 (8s)", ko: "자동 갱신 (8s)" },

    // placeholder（會另外處理 placeholder 屬性，但 leaf 也可能出現）
    "搜尋卡片名稱...":  { en: "Search card name...",   ja: "カード名を検索...",   ko: "카드명 검색..." },
    "搜尋系列名稱...":  { en: "Search set name...",    ja: "シリーズ名を検索...", ko: "시리즈명 검색..." },
    "搜尋錢包地址…":    { en: "Search wallet address…", ja: "ウォレットを検索…",  ko: "지갑 주소 검색…" },
    "請輸入搜尋關鍵字": { en: "Enter search keyword",  ja: "検索キーワードを入力", ko: "검색어를 입력" },

    // 空狀態 / 提示
    "載入中…":   { en: "Loading…",   ja: "読み込み中…", ko: "불러오는 중…" },
    "載入中...":  { en: "Loading...", ja: "読み込み中...", ko: "불러오는 중..." },
    "⏳ 載入價格…": { en: "⏳ Loading prices…", ja: "⏳ 価格読み込み中…", ko: "⏳ 가격 로딩…" },
    "無資料":     { en: "No data",       ja: "データなし",   ko: "데이터 없음" },
    "無結果":     { en: "No results",    ja: "結果なし",     ko: "결과 없음" },
    "無紀錄":     { en: "No records",    ja: "記録なし",     ko: "기록 없음" },
    "無符合結果": { en: "No matches",    ja: "一致なし",     ko: "일치 없음" },
    "無符合條件的資料": { en: "No matching data", ja: "該当データなし", ko: "해당 데이터 없음" },
    "無成交資料": { en: "No sales data", ja: "取引データなし", ko: "거래 데이터 없음" },
    "無獨立報價": { en: "No independent quote", ja: "独立見積なし", ko: "독립 시세 없음" },
    "暫無彩蛋資料": { en: "No easter-egg data yet", ja: "イースターエッグなし", ko: "이스터에그 없음" },
    "沒有符合條件的卡片": { en: "No matching cards", ja: "該当カードなし", ko: "해당 카드 없음" },
    "尚無限量卡機事件紀錄。": { en: "No limited-pack events yet.", ja: "限定パックイベントなし。", ko: "한정 팩 이벤트 없음." },
    "從左側選擇一張卡片": { en: "Pick a card on the left", ja: "左からカードを選択", ko: "왼쪽에서 카드 선택" },
    "系統將根據 CollectIQ 驗證價格": { en: "The system uses CollectIQ verified prices", ja: "CollectIQ 検証価格を使用", ko: "CollectIQ 검증 가격 사용" },
    "即時計算質押參數": { en: "to compute collateral params in real time", ja: "担保パラメータを即時計算", ko: "담보 파라미터 실시간 계산" },
    "點擊左側卡片加入組合（再次點擊移除）":
      { en: "Click a card on the left to add (click again to remove)",
        ja: "左のカードをクリックで追加（再クリックで削除）",
        ko: "왼쪽 카드 클릭으로 추가 (다시 클릭 시 제거)" },
    "← 表格可左右滑動查看全部欄位 →":
      { en: "← Swipe the table to see all columns →",
        ja: "← 表を左右にスワイプで全列表示 →",
        ko: "← 표를 좌우로 스크롤하여 모든 열 보기 →" },

    // 信心 / 狀態徽章
    "🟢 高信心": { en: "🟢 High", ja: "🟢 高信頼", ko: "🟢 높음" },
    "🟡 中信心": { en: "🟡 Medium", ja: "🟡 中信頼", ko: "🟡 중간" },
    "🔴 低信心": { en: "🔴 Low", ja: "🔴 低信頼", ko: "🔴 낮음" },
    "高信心":   { en: "High conf.",   ja: "高信頼",   ko: "높은 신뢰" },
    "中信心":   { en: "Medium conf.", ja: "中信頼",   ko: "중간 신뢰" },
    "低信心":   { en: "Low conf.",    ja: "低信頼",   ko: "낮은 신뢰" },
    "✅ 相符":  { en: "✅ Match",  ja: "✅ 一致",   ko: "✅ 일치" },
    "❌ 失敗":  { en: "❌ Fail",   ja: "❌ 失敗",   ko: "❌ 실패" },
    "抽出":     { en: "Pulled",   ja: "抽選",     ko: "뽑음" },
    "未識別":   { en: "Unknown",  ja: "未識別",   ko: "미식별" },

    // ── 首頁 index / 總覽 ──
    "Renaiss 平台定價可信度": { en: "Renaiss Pricing Trust", ja: "Renaiss 価格の信頼度", ko: "Renaiss 가격 신뢰도" },
    "監控卡片數":   { en: "Cards Tracked",    ja: "監視カード数",   ko: "추적 카드 수" },
    "已驗證覆蓋率": { en: "Verified Coverage", ja: "検証カバレッジ", ko: "검증 커버리지" },
    "彩蛋機會":     { en: "Easter Eggs",      ja: "イースターエッグ", ko: "이스터에그" },
    "偏高警告":     { en: "Overpriced Warn",  ja: "高値警告",       ko: "고평가 경고" },
    "監控總市值":   { en: "Total Market Cap", ja: "総時価総額",     ko: "총 시가총액" },
    "📊 定價分佈":  { en: "📊 Price Distribution", ja: "📊 価格分布", ko: "📊 가격 분포" },
    "低估（彩蛋）": { en: "Underpriced (egg)", ja: "過小評価（エッグ）", ko: "저평가 (에그)" },
    "相符 ±10%":    { en: "Match ±10%", ja: "一致 ±10%", ko: "일치 ±10%" },
    "偏高":         { en: "Overpriced", ja: "高値",     ko: "고평가" },
    "偏高（>10%）": { en: "Overpriced (>10%)", ja: "高値（>10%）", ko: "고평가 (>10%)" },
    "未驗證":       { en: "Unverified", ja: "未検証",   ko: "미검증" },
    "🥚 Top 彩蛋排行": { en: "🥚 Top Easter Eggs", ja: "🥚 トップ・イースターエッグ", ko: "🥚 톱 이스터에그" },
    "Renaiss 低估最多": { en: "Renaiss most underpriced", ja: "Renaiss 過小評価トップ", ko: "Renaiss 최다 저평가" },
    "官方EV":       { en: "Official EV", ja: "公式EV",  ko: "공식 EV" },
    "官方 EV":      { en: "Official EV", ja: "公式EV",  ko: "공식 EV" },
    "在":           { en: "In",  ja: "うち",  ko: "총" },
    "張已驗證的卡片中，": { en: "verified cards, of which", ja: "枚の検証済みカードのうち、", ko: "개의 검증된 카드 중," },
    "的 Renaiss 定價與外部市場價差在 ±30% 以內。":
      { en: "of Renaiss prices are within ±30% of the external market.",
        ja: "の Renaiss 価格が外部市場と ±30% 以内。",
        ko: "의 Renaiss 가격이 외부 시장과 ±30% 이내." },
    "嚴格準確率（±10%）：": { en: "Strict accuracy (±10%): ", ja: "厳密精度（±10%）：", ko: "엄격 정확도(±10%): " }
  };

  // ---- 內插字串（含數字/資料）用 regex pattern 處理 ----
  // 每筆：re + 各語言 replacement（用 $1..$n 帶入 capture group）
  const PATTERNS = [
    { re: /^(\d[\d,]*) 系列 · (\d[\d,]*) 張 · (\d[\d,]*) 已驗證$/,
      en: "$1 sets · $2 cards · $3 verified", ja: "$1 シリーズ · $2 枚 · $3 検証済み", ko: "$1 시리즈 · $2 장 · $3 검증됨" },
    { re: /^\((\d[\d,]*) 張可質押\)$/,
      en: "($1 pledgeable)", ja: "($1 枚担保可)", ko: "($1 장 담보 가능)" },
    { re: /^資料更新：(.+)$/,
      en: "Updated: $1", ja: "更新: $1", ko: "업데이트: $1" },
    { re: /^潛在價差 (\$.+)$/,
      en: "Potential gap $1", ja: "潜在価格差 $1", ko: "잠재 가격차 $1" },
    { re: /^更新於 (.+)$/,
      en: "Updated $1", ja: "更新 $1", ko: "업데이트 $1" },
    { re: /^(.+ → )市場( \$.+)$/,
      en: "$1Market$2", ja: "$1市場価$2", ko: "$1시장가$2" }
  ];

  const LANGS = ["zh", "en", "ja", "ko"];
  const HTML_LANG = { zh: "zh-Hant", en: "en", ja: "ja", ko: "ko" };
  const LABEL = { zh: "中", en: "EN", ja: "日", ko: "한" };
  const SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1 };

  function getLang() {
    const l = localStorage.getItem("collectiq_lang");
    return LANGS.indexOf(l) >= 0 ? l : "zh";
  }
  function setLang(l) { localStorage.setItem("collectiq_lang", l); }

  // 保留原始前後空白，只換中間內容
  function reWhitespace(orig, translated) {
    const lead = (orig.match(/^\s*/) || [""])[0];
    const trail = (orig.match(/\s*$/) || [""])[0];
    return lead + translated + trail;
  }

  function translateNode(node, lang) {
    if (node.__i18n_zh === undefined) {
      const t = node.nodeValue;
      if (!t || !t.trim()) return;
      node.__i18n_zh = t;              // 首次見到 → 記住原文
    }
    const orig = node.__i18n_zh;
    const key = orig.trim();
    if (lang === "zh") {
      if (node.nodeValue !== orig) node.nodeValue = orig;
      return;
    }
    const entry = DICT[key];
    if (entry && entry[lang]) {
      node.nodeValue = reWhitespace(orig, entry[lang]);
      return;
    }
    for (let i = 0; i < PATTERNS.length; i++) {
      const p = PATTERNS[i];
      if (p[lang] && p.re.test(key)) {
        node.nodeValue = reWhitespace(orig, key.replace(p.re, p[lang]));
        return;
      }
    }
    if (node.nodeValue !== orig) node.nodeValue = orig; // 無翻譯 → 保留原文
  }

  function translateAttr(el, attr, cacheKey, lang) {
    if (el[cacheKey] === undefined) {
      const v = el.getAttribute(attr);
      if (!v || !v.trim()) return;
      el[cacheKey] = v;
    }
    const orig = el[cacheKey];
    const entry = DICT[orig.trim()];
    if (lang !== "zh" && entry && entry[lang]) el.setAttribute(attr, entry[lang]);
    else el.setAttribute(attr, orig);
  }

  function walk(root, lang) {
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        const p = n.parentNode;
        if (!p || SKIP[p.nodeName]) return NodeFilter.FILTER_REJECT;
        if (p.closest && p.closest("#ciq-lang")) return NodeFilter.FILTER_REJECT;
        return n.nodeValue && n.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    const nodes = [];
    let n; while ((n = tw.nextNode())) nodes.push(n);
    nodes.forEach((nd) => translateNode(nd, lang));

    // placeholder / title 屬性
    const els = root.querySelectorAll ? root.querySelectorAll("[placeholder],[title]") : [];
    els.forEach((el) => {
      if (el.hasAttribute("placeholder")) translateAttr(el, "placeholder", "__i18n_ph", lang);
      if (el.hasAttribute("title")) translateAttr(el, "__i18n_ttl_attr", "__i18n_ttl", lang); // noop guard
    });
    els.forEach((el) => { if (el.hasAttribute("title")) translateAttr(el, "title", "__i18n_ttl", lang); });
  }

  function apply(lang) {
    document.documentElement.setAttribute("lang", HTML_LANG[lang] || "zh-Hant");
    walk(document.body, lang);
    document.querySelectorAll("#ciq-lang button").forEach((b) => {
      b.classList.toggle("active", b.dataset.lang === lang);
    });
  }

  function buildSwitcher() {
    if (document.getElementById("ciq-lang")) return;
    const box = document.createElement("div");
    box.id = "ciq-lang";
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "Language");
    LANGS.forEach((l) => {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.lang = l;
      b.textContent = LABEL[l];
      b.addEventListener("click", () => { setLang(l); apply(l); });
      box.appendChild(b);
    });
    const style = document.createElement("style");
    style.textContent =
      "#ciq-lang{position:fixed;top:8px;right:10px;z-index:9999;display:flex;gap:2px;" +
      "background:rgba(22,27,34,.92);border:1px solid #30363d;border-radius:8px;padding:3px;" +
      "backdrop-filter:blur(6px)}" +
      "#ciq-lang button{all:unset;cursor:pointer;font:600 12px/1 -apple-system,Segoe UI,sans-serif;" +
      "color:#8b949e;padding:5px 8px;border-radius:6px;min-width:24px;text-align:center}" +
      "#ciq-lang button:hover{color:#e6edf3}" +
      "#ciq-lang button.active{background:#1f6feb;color:#fff}" +
      "@media(max-width:480px){#ciq-lang{top:6px;right:6px}#ciq-lang button{padding:4px 6px;font-size:11px}}";
    document.head.appendChild(style);
    document.body.appendChild(box);
  }

  function observe(lang) {
    const mo = new MutationObserver((muts) => {
      const cur = getLang();
      if (cur === "zh") return;
      muts.forEach((m) => {
        m.addedNodes.forEach((nd) => {
          if (nd.nodeType === 3) translateNode(nd, cur);
          else if (nd.nodeType === 1) walk(nd, cur);
        });
      });
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }

  function init() {
    buildSwitcher();
    const lang = getLang();
    apply(lang);
    observe(lang);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  // 對外：讓內嵌 JS 可主動翻譯 / 查字典
  window.CIQ_I18N = {
    t(zh) { const e = DICT[zh]; const l = getLang(); return e && e[l] ? e[l] : zh; },
    lang: getLang,
    apply
  };
})();
