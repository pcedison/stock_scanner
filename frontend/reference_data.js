(function exposeReferenceData(root, factory) {
  const data = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = data;
  }
  if (root) {
    root.StockScannerReferenceData = data;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createReferenceData() {
  const DEFAULT_COMPANIES = [
    { stockCode: "2330", name: "台積電", market: "TWSE", industryName: "半導體業", isFinancial: false },
    { stockCode: "2357", name: "華碩", market: "TWSE", industryName: "電腦及週邊設備業", isFinancial: false },
    { stockCode: "2454", name: "聯發科", market: "TWSE", industryName: "半導體業", isFinancial: false },
    { stockCode: "3008", name: "大立光", market: "TWSE", industryName: "光電業", isFinancial: false },
    { stockCode: "5274", name: "信驊", market: "TPEX", industryName: "半導體業", isFinancial: false },
    { stockCode: "2881", name: "富邦金", market: "TWSE", industryName: "金融保險業", isFinancial: true },
  ];

  return {
    DEFAULT_COMPANIES,
  };
});
