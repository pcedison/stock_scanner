(function exposeNormalizeHelpers(root, factory) {
  const helpers = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = helpers;
  }
  if (root) {
    root.StockScannerNormalize = helpers;
  }
})(typeof globalThis !== "undefined" ? globalThis : null, function createNormalizeModule() {
  // Input / company / holding normalization and stock-input parsing.
  // Dependencies on app state are injected: `getCompanies()` returns the
  // current company universe and `defaultCompanies` is the bundled fallback.
  function createNormalize({ getCompanies, defaultCompanies }) {
    function normalizeText(value) {
      return String(value || "")
        .trim()
        .replace(/\s+/g, " ");
    }

    function safeText(value, fallback = "") {
      const normalized = normalizeText(value);
      return normalized || fallback;
    }

    function normalizeCompany(company) {
      if (!company || typeof company !== "object") return null;
      const stockCode = safeText(company.stockCode);
      if (!/^\d{4,6}$/.test(stockCode)) return null;
      return {
        stockCode,
        name: safeText(company.name || company.companyName, "未知公司"),
        market: safeText(company.market, "未標示市場"),
        industryName: safeText(company.industryName, "未標示產業"),
        isFinancial: Boolean(company.isFinancial),
      };
    }

    function normalizeCompanies(companies) {
      if (!Array.isArray(companies)) return [];
      return companies.map(normalizeCompany).filter(Boolean);
    }

    function companySource(companies = getCompanies()) {
      const normalized = normalizeCompanies(companies);
      return normalized.length ? normalized : normalizeCompanies(defaultCompanies);
    }

    function safeCompanyName(companyOrHolding) {
      if (!companyOrHolding) return "未知公司";
      const directName = safeText(companyOrHolding.name || companyOrHolding.companyName);
      if (directName) return directName;
      const company = findCompanyByCodeOrName(companyOrHolding.stockCode);
      return company ? company.name : "未知公司";
    }

    function optionalNumber(value) {
      if (value === null || value === undefined || value === "") return null;
      const number = Number(value);
      return Number.isFinite(number) ? Math.max(0, number) : null;
    }

    function findCompanyByCodeOrName(query, companies = getCompanies()) {
      const normalized = normalizeText(query).toLowerCase();
      if (!normalized) return null;
      const source = companySource(companies);
      return (
        source.find((company) => company.stockCode.toLowerCase() === normalized) ||
        source.find((company) => company.name.toLowerCase() === normalized) ||
        source.find((company) =>
          `${company.stockCode} ${company.name} ${company.market} ${company.industryName}`
            .toLowerCase()
            .includes(normalized),
        ) ||
        null
      );
    }

    function parseStockInput(input, companies = getCompanies()) {
      const normalized = normalizeText(input);
      if (!normalized) {
        return { ok: false, error: "請輸入股票代碼或公司名稱" };
      }

      const tokens = normalized.split(" ");
      const codeToken = tokens.find((token) => /^\d{4,6}$/.test(token));
      const numericTokens = [];
      const nameTokens = [];
      let consumedCode = false;

      for (const token of tokens) {
        if (codeToken && token === codeToken && !consumedCode) {
          consumedCode = true;
          continue;
        }
        if (/^\d+(\.\d+)?$/.test(token)) {
          numericTokens.push(Number(token));
          continue;
        }
        nameTokens.push(token);
      }

      const nameQuery = nameTokens.join(" ");
      const company =
        findCompanyByCodeOrName(codeToken || "", companies) ||
        findCompanyByCodeOrName(nameQuery, companies) ||
        findCompanyByCodeOrName(normalized, companies);

      if (!company) {
        return { ok: false, error: "找不到資料" };
      }

      const shares = Number.isFinite(numericTokens[0]) ? Math.max(0, Math.floor(numericTokens[0])) : 0;
      const averageCost = Number.isFinite(numericTokens[1]) ? Math.max(0, numericTokens[1]) : null;
      return {
        ok: true,
        stockCode: company.stockCode,
        name: company.name,
        company,
        shares,
        averageCost,
      };
    }

    function normalizeHoldingRecord(item, companies = getCompanies()) {
      if (!item || typeof item !== "object") return null;
      const company = findCompanyByCodeOrName(item.stockCode || item.name, companies);
      if (!company) return null;
      const shares = Number(item.shares);
      const averageCost = Number(item.averageCost);
      return {
        stockCode: company.stockCode,
        name: company.name,
        shares: Number.isFinite(shares) ? Math.max(0, Math.floor(shares)) : 0,
        averageCost:
          item.averageCost === null ||
          item.averageCost === undefined ||
          item.averageCost === "" ||
          !Number.isFinite(averageCost)
            ? null
            : Math.max(0, averageCost),
      };
    }

    function normalizeHoldingRecords(records, companies = getCompanies()) {
      if (!Array.isArray(records)) return [];
      const deduped = new Map();
      records
        .map((item) => normalizeHoldingRecord(item, companies))
        .filter(Boolean)
        .forEach((holding) => deduped.set(holding.stockCode, holding));
      return [...deduped.values()].sort((a, b) => a.stockCode.localeCompare(b.stockCode));
    }

    return {
      normalizeText,
      safeText,
      normalizeCompany,
      normalizeCompanies,
      companySource,
      safeCompanyName,
      optionalNumber,
      findCompanyByCodeOrName,
      parseStockInput,
      normalizeHoldingRecord,
      normalizeHoldingRecords,
    };
  }

  return { createNormalize };
});
