export const DEFAULTS = Object.freeze({
  coin: "HYPE",
  assetId: 159,
  sizeDecimals: 2,
  maxLeverage: 10,
  lowerPrice: 30,
  upperPrice: 50,
  levels: 11,
  totalNotionalUsd: 100,
  infoUrl: "https://api.hyperliquid.xyz/info",
  wsUrl: "wss://api.hyperliquid.xyz/ws",
  entropyMarketsUrl: "https://entropy.io/api/markets",
  builderAddress: "0xcD254d2A328f7f67C7c6FEf930A4757516F7b601",
  builderFee: 0
});

function numberEnv(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value)) throw new Error(`${name} must be a finite number`);
  return value;
}

export function loadConfig() {
  return {
    ...DEFAULTS,
    lowerPrice: numberEnv("GRID_LOWER_PRICE", DEFAULTS.lowerPrice),
    upperPrice: numberEnv("GRID_UPPER_PRICE", DEFAULTS.upperPrice),
    levels: numberEnv("GRID_LEVELS", DEFAULTS.levels),
    totalNotionalUsd: numberEnv("GRID_TOTAL_NOTIONAL_USD", DEFAULTS.totalNotionalUsd),
    infoUrl: process.env.HL_INFO_URL || DEFAULTS.infoUrl,
    wsUrl: process.env.HL_WS_URL || DEFAULTS.wsUrl,
    entropyMarketsUrl: process.env.ENTROPY_MARKETS_URL || DEFAULTS.entropyMarketsUrl,
    builderAddress: process.env.ENTROPY_BUILDER_ADDRESS || DEFAULTS.builderAddress,
    builderFee: numberEnv("ENTROPY_BUILDER_FEE", DEFAULTS.builderFee)
  };
}
