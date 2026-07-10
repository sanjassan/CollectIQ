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
    "類型：": { en: "Type:", ja: "タイプ：", ko: "유형:" },
    "限量開放": { en: "Limited open", ja: "限定開放", ko: "한정 개방" },
    "新卡機": { en: "New pack", ja: "新パック", ko: "새 팩" },
    "抽出 S 卡": { en: "S-card pulled", ja: "S カード抽出", ko: "S 카드 배출" },
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
    "嚴格準確率（±10%）：": { en: "Strict accuracy (±10%): ", ja: "厳密精度（±10%）：", ko: "엄격 정확도(±10%): " },

    // ---- 價格驗證頁 (compare.html) ----
    "獨立驗證":     { en: "Independent verification", ja: "独立検証", ko: "독립 검증" },
    "±10% 以內。": { en: "within ±10%.", ja: "±10% 以内。", ko: "±10% 이내." },
    "：我們向 PriceCharting 獨立取得每張鑑定卡的公開成交價，與 Renaiss 官方 FMV 交叉驗證。":
      { en: ": we independently pull each graded card's public sold price from PriceCharting and cross-check it against Renaiss's official FMV.",
        ja: "：各鑑定カードの公開取引価格を PriceCharting から独立取得し、Renaiss 公式 FMV と照合します。",
        ko: ": 각 등급 카드의 공개 체결가를 PriceCharting에서 독립적으로 가져와 Renaiss 공식 FMV와 대조합니다." },
    "= (外部市場價 − Renaiss FMV) / Renaiss FMV。":
      { en: "= (external market price − Renaiss FMV) / Renaiss FMV.",
        ja: "= (外部市場価 − Renaiss FMV) / Renaiss FMV。",
        ko: "= (외부 시장가 − Renaiss FMV) / Renaiss FMV." },
    "外面更貴（彩蛋機會）·": { en: "pricier elsewhere (easter egg) ·", ja: "外の方が高い（エッグ）·", ko: "외부가 더 비쌈 (에그) ·" },
    "外面更便宜 ·": { en: "cheaper elsewhere ·", ja: "外の方が安い ·", ko: "외부가 더 쌈 ·" },
    "以內。":       { en: "or less.", ja: "以内。", ko: "이내." },
    "🥚 低估":      { en: "🥚 Underpriced", ja: "🥚 過小評価", ko: "🥚 저평가" },
    "✅ 相符":      { en: "✅ Match", ja: "✅ 一致", ko: "✅ 일치" },
    "🥚 低估（彩蛋）": { en: "🥚 Underpriced (egg)", ja: "🥚 過小評価（エッグ）", ko: "🥚 저평가 (에그)" },
    "⚠️ 偏高":      { en: "⚠️ Overpriced", ja: "⚠️ 高値", ko: "⚠️ 고평가" },
    "— 未驗證":     { en: "— Unverified", ja: "— 未検証", ko: "— 미검증" },
    "🔄 鑑定商換算計算機": { en: "🔄 Grader Conversion Calculator", ja: "🔄 鑑定会社換算計算機", ko: "🔄 등급사 환산 계산기" },
    "同一張卡同分數，任一格輸入價格即時換算。係數：PSA=100%，可在 our_price.py 調整。":
      { en: "Same card, same grade — type a price in any box for instant conversion. Coefficients: PSA=100%, adjustable in our_price.py.",
        ja: "同一カード・同一グレード。いずれかの欄に価格を入力すると即時換算。係数：PSA=100%、our_price.py で調整可。",
        ko: "동일 카드·동일 등급 — 아무 칸에나 가격을 입력하면 즉시 환산. 계수: PSA=100%, our_price.py에서 조정 가능." },
    "PriceCharting · 獨立": { en: "PriceCharting · independent", ja: "PriceCharting · 独立", ko: "PriceCharting · 독립" },
    "PriceCharting 彙整 eBay 成交，獨立可查證":
      { en: "PriceCharting aggregates eBay sales — independent and verifiable",
        ja: "PriceCharting は eBay 取引を集約、独立して検証可能",
        ko: "PriceCharting은 eBay 체결을 집계 — 독립적이고 검증 가능" },

    // ---- Price Intel 頁 (intelligence.html) ----
    "⚙️ 背景數據建置中": { en: "⚙️ Building data in background", ja: "⚙️ バックグラウンドでデータ構築中", ko: "⚙️ 백그라운드 데이터 구축 중" },
    "Token 總數":   { en: "Total Tokens", ja: "トークン総数", ko: "총 토큰 수" },
    "Renaiss FMV 虛高 >100%": { en: "Renaiss FMV inflated >100%", ja: "Renaiss FMV 過大 >100%", ko: "Renaiss FMV 과대 >100%" },
    "Renaiss FMV 低估 <−20%": { en: "Renaiss FMV underpriced <−20%", ja: "Renaiss FMV 過小 <−20%", ko: "Renaiss FMV 저평가 <−20%" },
    "平均 FMV 落差%": { en: "Avg FMV gap %", ja: "平均 FMV 乖離%", ko: "평균 FMV 격차%" },
    "Renaiss 總 FMV (USD)": { en: "Total Renaiss FMV (USD)", ja: "Renaiss 総 FMV (USD)", ko: "총 Renaiss FMV (USD)" },
    "真實市場總 FMV (USD)": { en: "Total Real-Market FMV (USD)", ja: "実市場総 FMV (USD)", ko: "실시장 총 FMV (USD)" },
    "📊 FMV 落差排行": { en: "📊 FMV Gap Ranking", ja: "📊 FMV 乖離ランキング", ko: "📊 FMV 격차 순위" },
    "🐋 鯨魚錢包": { en: "🐋 Whale Wallets", ja: "🐋 クジラウォレット", ko: "🐋 고래 지갑" },
    "🎲 開包 True EV": { en: "🎲 Pack True EV", ja: "🎲 開封 True EV", ko: "🎲 팩 True EV" },
    "Renaiss 虛高 (gap > 0)": { en: "Renaiss inflated (gap > 0)", ja: "Renaiss 過大 (gap > 0)", ko: "Renaiss 과대 (gap > 0)" },
    "Renaiss 低估 (gap < 0)": { en: "Renaiss underpriced (gap < 0)", ja: "Renaiss 過小 (gap < 0)", ko: "Renaiss 저평가 (gap < 0)" },
    "大落差 (>100% or <−30%)": { en: "Big gap (>100% or <−30%)", ja: "大乖離 (>100% or <−30%)", ko: "큰 격차 (>100% or <−30%)" },
    "高信心（Index 高信心）": { en: "High confidence (Index high)", ja: "高信頼（Index 高）", ko: "고신뢰 (Index 높음)" },
    "卡名":         { en: "Card", ja: "カード名", ko: "카드명" },
    "真實市場價":  { en: "Real Market Price", ja: "実市場価", ko: "실시장가" },
    "落差%":        { en: "Gap %", ja: "乖離%", ko: "격차%" },
    "落差 USD":     { en: "Gap USD", ja: "乖離 USD", ko: "격차 USD" },
    "信心":         { en: "Confidence", ja: "信頼度", ko: "신뢰도" },
    "最近成交":     { en: "Last Sale", ja: "直近取引", ko: "최근 체결" },
    "連結":         { en: "Link", ja: "リンク", ko: "링크" },
    "FMV虛高":      { en: "FMV inflated", ja: "FMV 過大", ko: "FMV 과대" },
    "低估":         { en: "Underpriced", ja: "過小評価", ko: "저평가" },
    "錢包地址":     { en: "Wallet Address", ja: "ウォレットアドレス", ko: "지갑 주소" },
    "真實 FMV":     { en: "Real FMV", ja: "実 FMV", ko: "실 FMV" },
    "平均落差%":    { en: "Avg Gap %", ja: "平均乖離%", ko: "평균 격차%" },
    "帳面風險 USD": { en: "Paper Risk USD", ja: "含み損リスク USD", ko: "장부 위험 USD" },
    "最高值持倉":   { en: "Top Holding", ja: "最高額保有", ko: "최고가 보유" },
    "開抽價":       { en: "Pack Price", ja: "開封価格", ko: "개봉가" },
    "頭獎 FMV":     { en: "Jackpot FMV", ja: "大当り FMV", ko: "잭팟 FMV" },
    "搜尋卡名 / serial…": { en: "Search card name / serial…", ja: "カード名 / シリアル検索…", ko: "카드명 / 시리얼 검색…" },
    "搜尋錢包地址…": { en: "Search wallet address…", ja: "ウォレットアドレス検索…", ko: "지갑 주소 검색…" },
    "⚠️ 經驗 EV 使用 Renaiss 官方 FMV 計算。若 FMV 整體虛高，實際 EV 可能更低。":
      { en: "⚠️ Empirical EV is computed with Renaiss's official FMV. If FMV is broadly inflated, real EV may be lower.",
        ja: "⚠️ 経験 EV は Renaiss 公式 FMV で算出。FMV が全体的に過大なら実際の EV はさらに低い可能性。",
        ko: "⚠️ 경험 EV는 Renaiss 공식 FMV로 계산. FMV가 전반적으로 과대하면 실제 EV는 더 낮을 수 있음." },

    // ---- Oracle 頁 (oracle.html) ----
    "獨立的定價公信力": { en: "independent pricing credibility", ja: "独立した価格の信頼性", ko: "독립적인 가격 신뢰성" },
    "。": { en: ".", ja: "。", ko: "." },
    "CollectIQ 提供經過外部市場交叉驗證的價格，讓每張 RWA 都有可信的抵押價值。":
      { en: "CollectIQ supplies prices cross-verified against the external market, giving every RWA a trustworthy collateral value.",
        ja: "CollectIQ は外部市場と照合済みの価格を提供し、あらゆる RWA に信頼できる担保価値を与えます。",
        ko: "CollectIQ는 외부 시장과 교차 검증된 가격을 제공하여 모든 RWA에 신뢰할 수 있는 담보 가치를 부여합니다." },
    "鑑定卡上鏈": { en: "Graded cards on-chain", ja: "鑑定カードをオンチェーン化", ko: "등급 카드 온체인" },
    "獨立價格驗證": { en: "Independent price verification", ja: "独立価格検証", ko: "독립 가격 검증" },
    "質押合約": { en: "Staking Contract", ja: "ステーキング契約", ko: "스테이킹 계약" },
    "動態 LTV / 清算": { en: "Dynamic LTV / liquidation", ja: "動的 LTV / 清算", ko: "동적 LTV / 청산" },
    "DeFi 組合": { en: "DeFi Composability", ja: "DeFi コンポーザビリティ", ko: "DeFi 조합성" },
    "借貸 / 流動性池": { en: "Lending / liquidity pools", ja: "貸借 / 流動性プール", ko: "대출 / 유동성 풀" },
    "Oracle 驗證卡片": { en: "Oracle-Verified Cards", ja: "Oracle 検証カード", ko: "Oracle 검증 카드" },
    "可質押總價值": { en: "Total Pledgeable Value", ja: "担保可能総額", ko: "총 담보 가능 가치" },
    "組合總借款額度": { en: "Portfolio Total Borrow Limit", ja: "ポートフォリオ総借入枠", ko: "포트폴리오 총 대출 한도" },
    "平均 LTV": { en: "Average LTV", ja: "平均 LTV", ko: "평균 LTV" },
    "高信心卡片": { en: "High-Confidence Cards", ja: "高信頼カード", ko: "고신뢰 카드" },
    "📊 動態 LTV 分級": { en: "📊 Dynamic LTV Tiers", ja: "📊 動的 LTV 分級", ko: "📊 동적 LTV 등급" },
    "依驗證信心度調整": { en: "adjusted by verification confidence", ja: "検証信頼度で調整", ko: "검증 신뢰도로 조정" },
    "Renaiss FMV 與外部市場價差": { en: "Renaiss FMV vs external-market gap", ja: "Renaiss FMV と外部市場の乖離", ko: "Renaiss FMV와 외부 시장 격차" },
    "±10% 以內": { en: "within ±10%", ja: "±10% 以内", ko: "±10% 이내" },
    "定價可信 → 質押成數最高": { en: "Pricing trustworthy → highest LTV", ja: "価格信頼 → 担保率最高", ko: "가격 신뢰 → 담보 비율 최고" },
    "張卡片適用": { en: "cards apply", ja: "枚が該当", ko: "장 적용" },
    "價差在": { en: "Gap in the", ja: "乖離が", ko: "격차가" },
    "之間": { en: "range", ja: "の範囲", ko: "범위" },
    "有偏差但仍在合理範圍": { en: "Some deviation but still reasonable", ja: "偏差はあるが妥当範囲内", ko: "편차 있으나 합리적 범위" },
    "價差": { en: "Gap", ja: "乖離", ko: "격차" },
    "超過 30%": { en: "over 30%", ja: "30% 超", ko: "30% 초과" },
    "定價偏離嚴重 → 保守質押": { en: "Pricing deviates heavily → conservative LTV", ja: "価格乖離が大きい → 保守的担保", ko: "가격 편차 큼 → 보수적 담보" },
    "向 PriceCharting 獨立取得每張鑑定卡的公開成交價，與 Renaiss FMV 交叉驗證。目前已覆蓋 520+ 張卡。":
      { en: "Independently pull each graded card's public sold price from PriceCharting and cross-check against Renaiss FMV. Currently covers 520+ cards.",
        ja: "各鑑定カードの公開取引価格を PriceCharting から独立取得し、Renaiss FMV と照合。現在 520+ 枚をカバー。",
        ko: "각 등급 카드의 공개 체결가를 PriceCharting에서 독립 취득해 Renaiss FMV와 대조. 현재 520+ 장 커버." },
    "✅ 已上線": { en: "✅ Live", ja: "✅ 稼働中", ko: "✅ 가동 중" },
    "鑑定商換算": { en: "Grader Conversion", ja: "鑑定会社換算", ko: "등급사 환산" },
    "PSA / CGC / BGS 同分卡的市場價差可達 40%。Oracle 內建換算係數，確保跨鑑定商的公平定價。":
      { en: "Same-grade PSA / CGC / BGS cards can differ by up to 40% in market price. The Oracle has built-in conversion coefficients for fair cross-grader pricing.",
        ja: "同グレードの PSA / CGC / BGS カードは市場価が最大 40% 異なります。Oracle は換算係数を内蔵し、鑑定会社をまたいだ公平な価格を保証。",
        ko: "동일 등급 PSA / CGC / BGS 카드는 시장가가 최대 40% 차이납니다. Oracle은 환산 계수를 내장해 등급사 간 공정한 가격을 보장." },
    "鏈上 Oracle Feed": { en: "On-Chain Oracle Feed", ja: "オンチェーン Oracle フィード", ko: "온체인 Oracle 피드" },
    "將驗證後的價格推上鏈，質押合約可直接讀取。Smart contract 根據 confidence 動態調整 LTV。":
      { en: "Push verified prices on-chain so staking contracts can read them directly. Smart contracts adjust LTV dynamically by confidence.",
        ja: "検証済み価格をオンチェーンにプッシュし、ステーキング契約が直接読み取り可能。スマートコントラクトが信頼度に応じて LTV を動的調整。",
        ko: "검증된 가격을 온체인에 푸시해 스테이킹 계약이 직접 읽을 수 있음. 스마트 컨트랙트가 신뢰도에 따라 LTV를 동적 조정." },
    "DeFi 可組合性": { en: "DeFi Composability", ja: "DeFi コンポーザビリティ", ko: "DeFi 조합성" },
    "有了可信的鏈上價格，Aave/Compound-style 的 RWA 借貸協議就有接入基礎。卡片從收藏品升級為金融工具。":
      { en: "With trustworthy on-chain prices, Aave/Compound-style RWA lending protocols have a foundation to plug into. Cards upgrade from collectibles to financial instruments.",
        ja: "信頼できるオンチェーン価格があれば、Aave/Compound 型の RWA 貸借プロトコルが接続できる土台に。カードは収集品から金融商品へ昇格。",
        ko: "신뢰할 수 있는 온체인 가격이 있으면 Aave/Compound 방식의 RWA 대출 프로토콜이 연결될 기반이 됩니다. 카드는 수집품에서 금융 상품으로 격상." },
    "🏦 質押模擬": { en: "🏦 Staking Simulation", ja: "🏦 ステーキング・シミュレーション", ko: "🏦 스테이킹 시뮬레이션" },
    "以真實驗證數據計算": { en: "computed from real verification data", ja: "実検証データで算出", ko: "실제 검증 데이터로 계산" },
    "每張卡的質押參數由 Oracle 根據外部驗證價和信心度即時計算。":
      { en: "Each card's staking parameters are computed in real time by the Oracle from external verified prices and confidence.",
        ja: "各カードのステーキングパラメータは、外部検証価と信頼度から Oracle がリアルタイム算出。",
        ko: "각 카드의 스테이킹 파라미터는 외부 검증가와 신뢰도를 기반으로 Oracle이 실시간 계산." },
    "驗證價": { en: "Verified Price", ja: "検証価", ko: "검증가" },
    "= 外部市場成交價（非 Renaiss FMV）·": { en: "= external market sold price (not Renaiss FMV) ·", ja: "= 外部市場取引価（Renaiss FMV ではない）·", ko: "= 외부 시장 체결가 (Renaiss FMV 아님) ·" },
    "= 可質押比例 ·": { en: "= pledgeable ratio ·", ja: "= 担保可能比率 ·", ko: "= 담보 가능 비율 ·" },
    "清算價": { en: "Liquidation Price", ja: "清算価", ko: "청산가" },
    "= 驗證價 × 80%（跌破此價觸發清算）": { en: "= verified price × 80% (falling below triggers liquidation)", ja: "= 検証価 × 80%（これを下回ると清算）", ko: "= 검증가 × 80% (이 가격 하회 시 청산)" },
    "💡 為什麼不能用 Renaiss FMV 直接質押？": { en: "💡 Why not stake directly on Renaiss FMV?", ja: "💡 なぜ Renaiss FMV で直接ステーキングできないのか？", ko: "💡 왜 Renaiss FMV로 바로 스테이킹할 수 없나?" },
    "❌ 平台自己定價": { en: "❌ Platform prices itself", ja: "❌ プラットフォームが自ら価格設定", ko: "❌ 플랫폼이 스스로 가격 책정" },
    "平台既是發行者又是定價者 → 利益衝突": { en: "Platform is both issuer and pricer → conflict of interest", ja: "発行者と価格決定者を兼ねる → 利益相反", ko: "발행자이자 가격 결정자 → 이해 상충" },
    "無法排除 FMV 被高估的可能": { en: "Can't rule out FMV being overstated", ja: "FMV が過大評価される可能性を排除できない", ko: "FMV 과대 평가 가능성을 배제할 수 없음" },
    "外部 DeFi 協議不會信任平台自報價格": { en: "External DeFi protocols won't trust self-reported prices", ja: "外部 DeFi プロトコルは自己申告価格を信頼しない", ko: "외부 DeFi 프로토콜은 자체 신고 가격을 신뢰하지 않음" },
    "等同銀行自己替自己的資產估價": { en: "Like a bank appraising its own assets", ja: "銀行が自らの資産を評価するのと同じ", ko: "은행이 자기 자산을 스스로 평가하는 것과 같음" },
    "✅ CollectIQ 獨立驗證": { en: "✅ CollectIQ independent verification", ja: "✅ CollectIQ 独立検証", ko: "✅ CollectIQ 독립 검증" },
    "價格來自 PriceCharting 公開成交數據": { en: "Prices come from PriceCharting public sales data", ja: "価格は PriceCharting の公開取引データ由来", ko: "가격은 PriceCharting 공개 체결 데이터에서 유래" },
    "交叉驗證產生 confidence score": { en: "Cross-verification yields a confidence score", ja: "クロス検証で confidence score を生成", ko: "교차 검증으로 confidence score 생성" },
    "LTV 根據驗證結果動態調整": { en: "LTV adjusts dynamically by verification result", ja: "LTV は検証結果に応じて動的調整", ko: "LTV는 검증 결과에 따라 동적 조정" },
    "等同第三方估價師 → DeFi 協議可信任": { en: "Like a third-party appraiser → DeFi protocols can trust it", ja: "第三者評価者に相当 → DeFi プロトコルが信頼可能", ko: "제3자 감정평가사에 해당 → DeFi 프로토콜이 신뢰 가능" },
    "信心度": { en: "Confidence", ja: "信頼度", ko: "신뢰도" },
    "可質押價值": { en: "Pledgeable Value", ja: "担保可能額", ko: "담보 가능 가치" },
    "🟢 高": { en: "🟢 High", ja: "🟢 高", ko: "🟢 높음" },
    "🟡 中": { en: "🟡 Medium", ja: "🟡 中", ko: "🟡 중간" },
    "🔴 低": { en: "🔴 Low", ja: "🔴 低", ko: "🔴 낮음" },

    // ---- 鏈上持有頁 (holdings.html) ----
    "搜尋卡名 / 持有者地址…": { en: "Search card name / holder address…", ja: "カード名 / 保有者アドレス検索…", ko: "카드명 / 보유자 주소 검색…" },
    "FMV 高→低":   { en: "FMV high→low", ja: "FMV 高→低", ko: "FMV 높음→낮음" },
    "最近轉移":     { en: "Recently transferred", ja: "直近の移転", ko: "최근 이전" },
    "無圖片":       { en: "No image", ja: "画像なし", ko: "이미지 없음" },
    "📍 存放位置：": { en: "📍 Location: ", ja: "📍 保管場所：", ko: "📍 보관 위치: " },

    // ---- Live Pool 頁 (live.html) ----
    "限量卡機開放窗口 · 鏈上獎池即時追蹤": { en: "Limited-machine open window · live on-chain pool tracking", ja: "限定マシン開放ウィンドウ · オンチェーン賞金プール即時追跡", ko: "한정 머신 개방 창 · 온체인 상금 풀 실시간 추적" },
    "卡池：":       { en: "Pool:", ja: "プール：", ko: "풀:" },
    "自動更新 (8s)": { en: "Auto-refresh (8s)", ja: "自動更新 (8s)", ko: "자동 새로고침 (8s)" },
    "距開放":       { en: "Until open", ja: "開放まで", ko: "개방까지" },
    "抽卡進度":     { en: "Pull Progress", ja: "抽選進捗", ko: "뽑기 진행" },
    "即時抽卡事件流": { en: "Live Pull Event Stream", ja: "リアルタイム抽選イベント", ko: "실시간 뽑기 이벤트 스트림" },
    "🐋 抽最多的錢包": { en: "🐋 Top-Pulling Wallets", ja: "🐋 最多抽選ウォレット", ko: "🐋 최다 뽑기 지갑" },
    "📈 即時獎池 EV 反推（依已抽出的卡動態計算剩餘池價值）": { en: "📈 Live Pool EV Back-calc (remaining pool value from pulled cards)", ja: "📈 リアルタイム賞金プール EV 逆算（抽出済みカードから残プール価値を動的計算）", ko: "📈 실시간 상금 풀 EV 역산 (뽑힌 카드로 잔여 풀 가치 계산)" },
    "🍀 幸運卡價 / Easter Egg（Renaiss 標低、市場高 ≥1.5x · 仍在池可期待）": { en: "🍀 Lucky Prices / Easter Eggs (Renaiss low, market ≥1.5x · still in pool)", ja: "🍀 ラッキー価格 / イースターエッグ（Renaiss 低・市場 ≥1.5x · プール残存）", ko: "🍀 행운 가격 / 이스터에그 (Renaiss 낮음, 시장 ≥1.5x · 풀에 잔존)" },
    "♻️ 回收回池的高市值卡（避免回收錯誤 / 撿漏）": { en: "♻️ High-Value Cards Recycled Back (avoid mis-recycles / bargains)", ja: "♻️ プールに戻された高額カード（誤リサイクル回避 / お得）", ko: "♻️ 풀로 회수된 고가치 카드 (잘못된 회수 방지 / 득템)" },
    "⚠️ 疑似資料輸入錯誤（Renaiss ≫ 市場 3x 且信心低 → 別只信官方 FMV）": { en: "⚠️ Suspected Data-Entry Errors (Renaiss ≫ market 3x & low confidence → don't trust official FMV alone)", ja: "⚠️ データ入力ミス疑い（Renaiss ≫ 市場 3x かつ低信頼 → 公式 FMV だけを信じない）", ko: "⚠️ 데이터 입력 오류 의심 (Renaiss ≫ 시장 3x & 낮은 신뢰 → 공식 FMV만 믿지 말 것)" },
    "💎 大獎已被抽走 (FMV≥$300)": { en: "💎 Big Prizes Already Pulled (FMV≥$300)", ja: "💎 抽出済みの大当り (FMV≥$300)", ko: "💎 이미 뽑힌 대박 (FMV≥$300)" },
    "🎁 大獎仍在池內 (可期待)": { en: "🎁 Big Prizes Still in Pool (expectable)", ja: "🎁 プールに残る大当り（期待可）", ko: "🎁 풀에 남은 대박 (기대 가능)" },
    "時間(UTC)":    { en: "Time (UTC)", ja: "時刻(UTC)", ko: "시간(UTC)" },
    "圖":           { en: "Img", ja: "画像", ko: "이미지" },
    "落差":         { en: "Gap", ja: "乖離", ko: "격차" },
    "抽卡數":       { en: "Pulls", ja: "抽選数", ko: "뽑기 수" },
    "被誰抽走":     { en: "Pulled By", ja: "抽出者", ko: "뽑은 사람" },
    "灌入池中":     { en: "Loaded into pool", ja: "プール投入", ko: "풀 투입" },
    "仍在池內":     { en: "Still in pool", ja: "プール残存", ko: "풀에 잔존" },
    "已被抽走":     { en: "Pulled", ja: "抽出済み", ko: "뽑힘" },
    "回收回池":     { en: "Recycled back", ja: "プールに戻し", ko: "풀로 회수" },
    "已銷毀":       { en: "Burned", ja: "焼却済み", ko: "소각됨" },
    "買家錢包":     { en: "Buyer wallets", ja: "購入者ウォレット", ko: "구매자 지갑" },
    "💎在池":       { en: "💎 in pool", ja: "💎 プール内", ko: "💎 풀 내" },
    "💎已抽走":     { en: "💎 pulled", ja: "💎 抽出済", ko: "💎 뽑힘" },
    "抽走":         { en: "Pulled", ja: "抽出", ko: "뽑음" },
    "回收":         { en: "Recycle", ja: "リサイクル", ko: "회수" },
    "銷毀":         { en: "Burn", ja: "焼却", ko: "소각" },
    "尚無事件":     { en: "No events yet", ja: "イベントなし", ko: "이벤트 없음" },
    "尚無大獎被抽走": { en: "No big prizes pulled yet", ja: "大当りの抽出はまだ", ko: "아직 뽑힌 대박 없음" },
    "剩餘池·市場EV": { en: "Remaining pool · market EV", ja: "残プール · 市場EV", ko: "잔여 풀 · 시장 EV" },
    "剩餘池·Renaiss EV": { en: "Remaining pool · Renaiss EV", ja: "残プール · Renaiss EV", ko: "잔여 풀 · Renaiss EV" },
    "已抽·市場均值": { en: "Pulled · market avg", ja: "抽出済 · 市場平均", ko: "뽑힘 · 시장 평균" },
    "卡機票價":     { en: "Machine ticket price", ja: "マシン料金", ko: "머신 티켓가" },
    "現在買·值回票價?": { en: "Buy now · worth the ticket?", ja: "今買う · 元は取れる?", ko: "지금 구매 · 본전?" },
    "尚無（覆蓋率補齊後浮現）": { en: "None yet (appears once coverage fills in)", ja: "なし（カバレッジ補完後に表示）", ko: "없음 (커버리지 보완 후 표시)" },
    "覆蓋率偏低，EV 僅供參考；新卡機的卡會隨 Index API 每日配額逐步補齊": { en: "Low coverage — EV is indicative only; new-machine cards fill in gradually via the Index API daily quota", ja: "カバレッジ低 — EV は参考値。新マシンのカードは Index API の日次配分で徐々に補完", ko: "커버리지 낮음 — EV는 참고용; 신규 머신 카드는 Index API 일일 할당으로 점진 보완" },
    "尚無資料。請跑": { en: "No data yet. Run", ja: "データなし。実行してください", ko: "데이터 없음. 실행" },
    "產生 live_pool.db。": { en: "to generate live_pool.db.", ja: "で live_pool.db を生成。", ko: "하여 live_pool.db 생성." },

    // ---- 外部比價頁 (price_search.html) ----
    "🔍 PriceCharting 外部價格搜尋": { en: "🔍 PriceCharting External Price Search", ja: "🔍 PriceCharting 外部価格検索", ko: "🔍 PriceCharting 외부 가격 검색" },
    "卡名 / 系列 / 卡號（混合搜尋）": { en: "Card / set / number (mixed search)", ja: "カード名 / セット / 番号（混合検索）", ko: "카드명 / 세트 / 번호 (혼합 검색)" },
    "例：Temporal Forces Charizard ex 125": { en: "e.g. Temporal Forces Charizard ex 125", ja: "例：Temporal Forces Charizard ex 125", ko: "예: Temporal Forces Charizard ex 125" },
    "分級機構":     { en: "Grader", ja: "鑑定機関", ko: "등급 기관" },
    "— 未分級 —":   { en: "— Ungraded —", ja: "— 未鑑定 —", ko: "— 미등급 —" },
    "搜尋":         { en: "Search", ja: "検索", ko: "검색" },
    "快速範例：":   { en: "Quick examples:", ja: "クイック例：", ko: "빠른 예시:" },
    "請輸入搜尋關鍵字": { en: "Please enter a search keyword", ja: "検索キーワードを入力してください", ko: "검색어를 입력하세요" },
    "⏳ 搜尋中…":   { en: "⏳ Searching…", ja: "⏳ 検索中…", ko: "⏳ 검색 중…" },
    "⏳ 載入價格…": { en: "⏳ Loading prices…", ja: "⏳ 価格読み込み中…", ko: "⏳ 가격 불러오는 중…" },
    "查無結果，試著換關鍵字。": { en: "No results — try different keywords.", ja: "結果なし — 別のキーワードをお試しください。", ko: "결과 없음 — 다른 검색어를 시도하세요." },
    "來源：":       { en: "Source: ", ja: "ソース：", ko: "출처: " },
    "查無價格":     { en: "No price found", ja: "価格なし", ko: "가격 없음" },
    "市場價格":     { en: "Market Price", ja: "市場価格", ko: "시장 가격" },
    "查無各等級價格": { en: "No per-grade prices found", ja: "グレード別価格なし", ko: "등급별 가격 없음" },
    "查詢等級":     { en: "queried grade", ja: "照会グレード", ko: "조회 등급" },

    // ---- API 狀態頁 (api_status.html) ----
    "▶ 執行全部驗證": { en: "▶ Run All Checks", ja: "▶ すべて検証を実行", ko: "▶ 전체 검증 실행" },
    "⏳ 驗證中…":   { en: "⏳ Checking…", ja: "⏳ 検証中…", ko: "⏳ 검증 중…" },
    "▶ 重新執行":   { en: "▶ Run Again", ja: "▶ 再実行", ko: "▶ 다시 실행" },
    "待測":         { en: "Pending", ja: "未検証", ko: "대기" },
    "❌ 失敗":      { en: "❌ Failed", ja: "❌ 失敗", ko: "❌ 실패" },
    "尚未執行":     { en: "Not run yet", ja: "未実行", ko: "미실행" },
    "所有卡機清單（含 EV、包價、頭獎 FMV）": { en: "All machines (with EV, pack price, jackpot FMV)", ja: "全マシン一覧（EV・パック価格・大当り FMV 含む）", ko: "전체 머신 목록 (EV·팩 가격·잭팟 FMV 포함)" },
    "Platform API — GET /packs/omega（含開包紀錄）": { en: "Platform API — GET /packs/omega (with open history)", ja: "Platform API — GET /packs/omega（開封履歴付き）", ko: "Platform API — GET /packs/omega (개봉 기록 포함)" },
    "最近 30 筆開包 FMV / tier": { en: "Last 30 opens: FMV / tier", ja: "直近 30 件の開封 FMV / tier", ko: "최근 30건 개봉 FMV / tier" },
    "市場掛單卡片（FMV、掛單價、持有者）": { en: "Marketplace listings (FMV, ask price, holder)", ja: "マーケット出品カード（FMV・出品価・保有者）", ko: "마켓 등록 카드 (FMV·호가·보유자)" },
    "外部市場定價搜尋（100 req/day 匿名限制）": { en: "External market pricing search (100 req/day anon limit)", ja: "外部市場価格検索（匿名 100 req/day 制限）", ko: "외부 시장 가격 검색 (익명 100 req/day 제한)" },
    "跨平台最近成交紀錄（snkrdunk 等）": { en: "Recent cross-platform sales (snkrdunk, etc.)", ja: "クロスプラットフォーム直近取引（snkrdunk 等）", ko: "크로스 플랫폼 최근 체결 (snkrdunk 등)" },
    "Pokemon TCG 整體市場指數": { en: "Pokemon TCG overall market index", ja: "Pokemon TCG 全体市場指数", ko: "Pokemon TCG 전체 시장 지수" },
    "回傳空陣列":   { en: "Returned empty array", ja: "空配列を返却", ko: "빈 배열 반환" },
    "卡機":         { en: "Machine", ja: "マシン", ko: "머신" },
    "包價":         { en: "Pack Price", ja: "パック価格", ko: "팩 가격" },
    "EV 倍率":      { en: "EV Ratio", ja: "EV 倍率", ko: "EV 배율" },
    "系列":         { en: "Set", ja: "セット", ko: "세트" },
    "外部市價":     { en: "External Price", ja: "外部市価", ko: "외부 시세" },
    "成交價":       { en: "Sold Price", ja: "取引価", ko: "체결가" },
    "平台":         { en: "Platform", ja: "プラットフォーム", ko: "플랫폼" },
    "日期":         { en: "Date", ja: "日付", ko: "날짜" },
    "無紀錄":       { en: "No records", ja: "記録なし", ko: "기록 없음" },
    "無結果":       { en: "No results", ja: "結果なし", ko: "결과 없음" },
    "查詢：":       { en: "Query: ", ja: "クエリ：", ko: "쿼리: " }
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
    { re: /^更新於 ([^·]+)$/,
      en: "Updated $1", ja: "更新 $1", ko: "업데이트 $1" },
    { re: /^(.+ → )市場( \$.+)$/,
      en: "$1Market$2", ja: "$1市場価$2", ko: "$1시장가$2" },
    // 價格驗證頁 meta（含「部分」字樣）
    { re: /^(.+) · 共 (\d[\d,]*) 張 · 已驗證 (\d[\d,]*) 張（部分，背景仍在跑） · 低估 (\d[\d,]*) \/ 偏高 (\d[\d,]*) \/ 相符 (\d[\d,]*) \/ 未驗證 (\d[\d,]*)$/,
      en: "$1 · $2 total · $3 verified (partial, still running) · underpriced $4 / overpriced $5 / match $6 / unverified $7",
      ja: "$1 · 計 $2 枚 · 検証済 $3 枚（部分・処理中） · 過小評価 $4 / 高値 $5 / 一致 $6 / 未検証 $7",
      ko: "$1 · 총 $2 장 · 검증 $3 장(부분, 진행 중) · 저평가 $4 / 고평가 $5 / 일치 $6 / 미검증 $7" },
    // 價格驗證頁 meta（完整）
    { re: /^(.+) · 共 (\d[\d,]*) 張 · 已驗證 (\d[\d,]*) 張 · 低估 (\d[\d,]*) \/ 偏高 (\d[\d,]*) \/ 相符 (\d[\d,]*) \/ 未驗證 (\d[\d,]*)$/,
      en: "$1 · $2 total · $3 verified · underpriced $4 / overpriced $5 / match $6 / unverified $7",
      ja: "$1 · 計 $2 枚 · 検証済 $3 枚 · 過小評価 $4 / 高値 $5 / 一致 $6 / 未検証 $7",
      ko: "$1 · 총 $2 장 · 검증 $3 장 · 저평가 $4 / 고평가 $5 / 일치 $6 / 미검증 $7" },
    { re: /^載入失敗：(.+)$/,
      en: "Load failed: $1", ja: "読み込み失敗：$1", ko: "불러오기 실패: $1" },
    // Price Intel 頁
    { re: /^(\d[\d,]*) 筆$/, en: "$1 rows", ja: "$1 件", ko: "$1 건" },
    { re: /^(\d[\d,]*) 個錢包$/, en: "$1 wallets", ja: "$1 ウォレット", ko: "$1 지갑" },
    { re: /^經驗 EV（最近 (\d[\d,]*) 筆）$/,
      en: "Empirical EV (last $1)", ja: "経験 EV（直近 $1 件）", ko: "경험 EV (최근 $1건)" },
    { re: /^共 (\d[\d,]*)\/(\d[\d,]*) 枚已查真實市場價 ｜ 平均 Renaiss FMV 落差 (.+?) ｜ 更新於 (.+)$/,
      en: "$1/$2 tokens checked for real market price ｜ avg Renaiss FMV gap $3 ｜ updated $4",
      ja: "$1/$2 枚の実市場価を照会 ｜ 平均 Renaiss FMV 乖離 $3 ｜ 更新 $4",
      ko: "$1/$2 개 실시장가 조회 ｜ 평균 Renaiss FMV 격차 $3 ｜ 업데이트 $4" },
    { re: /^📊 (\d[\d,]*)\/(\d[\d,]*) 已查 \(([\d.]+)%\) · (\d[\d,]*) 枚有真實市價$/,
      en: "📊 $1/$2 queried ($3%) · $4 with real market price",
      ja: "📊 $1/$2 照会済 ($3%) · $4 件が実市場価あり",
      ko: "📊 $1/$2 조회 ($3%) · $4 개 실시장가 보유" },
    { re: /^下次: 每日 08:10 台灣時間 · 每日 95 筆 · 約 (.+?) 天完成 · 配額重置於 (.+)$/,
      en: "Next: daily 08:10 TW · 95/day · ~$1 days to finish · quota resets $2",
      ja: "次回: 毎日 08:10 台湾時間 · 毎日 95 件 · 約 $1 日で完了 · 割当リセット $2",
      ko: "다음: 매일 08:10 대만시간 · 매일 95건 · 약 $1일 완료 · 할당 리셋 $2" },
    { re: /^下次: 每日 08:10 台灣時間 · 每日 95 筆 · 約 (.+?) 天完成$/,
      en: "Next: daily 08:10 TW · 95/day · ~$1 days to finish",
      ja: "次回: 毎日 08:10 台湾時間 · 毎日 95 件 · 約 $1 日で完了",
      ko: "다음: 매일 08:10 대만시간 · 매일 95건 · 약 $1일 완료" },
    { re: /^⚙️ 建置中 (\d[\d,]*)\/(\d[\d,]*) \(([\d.]+)%\)$/,
      en: "⚙️ Building $1/$2 ($3%)", ja: "⚙️ 構築中 $1/$2 ($3%)", ko: "⚙️ 구축 중 $1/$2 ($3%)" },
    { re: /^每日 95 筆配額，約 (.+?) 天完成$/,
      en: "95/day quota, ~$1 days to finish", ja: "毎日 95 件の割当、約 $1 日で完了", ko: "매일 95건 할당, 약 $1일 완료" },
    // API 狀態頁
    { re: /^共 (\d[\d,]*) 張掛單$/, en: "$1 listings", ja: "$1 件の出品", ko: "$1 개 등록" },
    { re: /^，共 (\d[\d,]*) 筆$/, en: ", $1 results", ja: "、$1 件", ko: ", $1 건" },
    { re: /^最近 (\d[\d,]*) 筆成交$/, en: "Last $1 sales", ja: "直近 $1 件の取引", ko: "최근 $1건 체결" },
    { re: /^\| 官方 EV:$/, en: "| Official EV:", ja: "| 公式 EV:", ko: "| 공식 EV:" },
    { re: /^\| 最近 (\d[\d,]*) 筆開包$/, en: "| last $1 opens", ja: "| 直近 $1 件の開封", ko: "| 최근 $1건 개봉" },
    // 限量歷史頁 (limited_history.html)
    { re: /^共 (\d[\d,]*) 筆$/, en: "$1 records", ja: "$1 件", ko: "$1 건" },
    { re: /^剩 ([\d,]+) 張$/, en: "$1 left", ja: "残り $1 枚", ko: "$1 장 남음" },
    { re: /^官方EV (\$[\d,.]+)$/, en: "Official EV $1", ja: "公式EV $1", ko: "공식 EV $1" },
    { re: /^S 卡：(.+)$/, en: "S-card: $1", ja: "S カード：$1", ko: "S 카드: $1" },
    // CDP 頁 (cdp.html) 動態
    { re: /^(.+) · 驗證價 (\$[\d,.]+)$/, en: "$1 · Verified $2", ja: "$1 · 検証価 $2", ko: "$1 · 검증가 $2" },
    // 外部比價頁
    { re: /^找到 (\d[\d,]*) 個結果 — 請選擇正確的卡片$/,
      en: "Found $1 results — pick the correct card", ja: "$1 件の結果 — 正しいカードを選択", ko: "$1 개 결과 — 올바른 카드를 선택" },
    { re: /^外部市場價（(.+)）$/,
      en: "External market price ($1)", ja: "外部市場価（$1）", ko: "외부 시장가 ($1)" },
    { re: /^❌ 連線失敗：(.+)$/,
      en: "❌ Connection failed: $1", ja: "❌ 接続失敗：$1", ko: "❌ 연결 실패: $1" },
    // 價格驗證頁 (compare.html) 動態
    { re: /^🔄 PSA (\$[\d,.]+) × (.+) 換算$/,
      en: "🔄 PSA $1 × $2 conversion", ja: "🔄 PSA $1 × $2 換算", ko: "🔄 PSA $1 × $2 환산" },
    // Live Pool 頁
    { re: /^更新於 (.+?) · 掃到區塊 (.+)$/,
      en: "Updated $1 · scanned to block $2", ja: "更新 $1 · ブロック $2 まで走査", ko: "업데이트 $1 · 블록 $2 까지 스캔" },
    { re: /^(.+) 開放時間 (.+)$/,
      en: "$1 opens at $2", ja: "$1 開放時刻 $2", ko: "$1 개방 시각 $2" },
    { re: /^(.+) \/ (.+) 抽出 \((.+?)\) · 剩 (.+) 在池$/,
      en: "$1 / $2 pulled ($3) · $4 left in pool", ja: "$1 / $2 抽出 ($3) · 残り $4 プール内", ko: "$1 / $2 뽑힘 ($3) · $4 풀에 남음" },
    { re: /^真實市場價覆蓋率 (\d[\d,]*)\/(\d[\d,]*) \((.+?)\)( —)?$/,
      en: "Real market-price coverage $1/$2 ($3)$4", ja: "実市場価カバレッジ $1/$2 ($3)$4", ko: "실시장가 커버리지 $1/$2 ($3)$4" },
    // 鏈上持有頁 meta
    { re: /^共 (\d[\d,]*) 張 token ｜ 已識別 (\d[\d,]*) ｜ 有圖 (\d[\d,]*) ｜ 更新於 (.+?) UTC ｜ to_addr = 鏈上當前存放位置$/,
      en: "$1 tokens ｜ identified $2 ｜ with image $3 ｜ updated $4 UTC ｜ to_addr = current on-chain location",
      ja: "計 $1 token ｜ 識別済 $2 ｜ 画像あり $3 ｜ 更新 $4 UTC ｜ to_addr = オンチェーン現在地",
      ko: "총 $1 token ｜ 식별 $2 ｜ 이미지 $3 ｜ 업데이트 $4 UTC ｜ to_addr = 온체인 현재 위치" },
    { re: /^未知卡片 #(\d+)(…?)$/, en: "Unknown card #$1$2", ja: "不明なカード #$1$2", ko: "알 수 없는 카드 #$1$2" },
    // Oracle 頁 hero（跨行）
    { re: /^社會能運作，是因為貨幣有公認的價值。[\s\S]*而是$/,
      en: "Society works because money has an agreed-upon value. For a card to become a pledgeable, lendable on-chain soft currency, what's needed isn't more features — it's ",
      ja: "社会が機能するのは、貨幣に共通の価値があるから。カードが担保・貸借可能なオンチェーンのソフトマネーになるのに必要なのは、機能ではなく — ",
      ko: "사회가 작동하는 것은 화폐에 공인된 가치가 있기 때문입니다. 카드가 담보·대출 가능한 온체인 소프트 통화가 되는 데 필요한 것은 더 많은 기능이 아니라 — " },
    { re: /^以下是每個卡機的 True EV 重算：[\s\S]*背後的真實風險。$/,
      en: "Below is a True-EV recomputation for each machine: Renaiss's official FMV is swapped for the Index API's real market price and expected value is recalculated, revealing the real risk behind the official EV.",
      ja: "以下は各マシンの True EV 再計算：Renaiss 公式 FMV を Index API の実市場価に置き換えて期待値を再計算し、公式 EV の裏にある実際のリスクを可視化します。",
      ko: "아래는 각 머신의 True EV 재계산: Renaiss 공식 FMV를 Index API 실시장가로 교체해 기대값을 다시 계산하여 공식 EV 뒤의 실제 위험을 보여줍니다." }
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
