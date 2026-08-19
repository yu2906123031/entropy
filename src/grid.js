function round(value, decimals) {
  const factor = 10 ** decimals;
  return Math.round((value + Number.EPSILON) * factor) / factor;
}

export function validateGridConfig(config) {
  if (!(config.lowerPrice > 0)) throw new Error("lower price must be positive");
  if (!(config.upperPrice > config.lowerPrice)) throw new Error("upper price must exceed lower price");
  if (!Number.isInteger(config.levels) || config.levels < 3) throw new Error("levels must be an integer >= 3");
  if (!(config.totalNotionalUsd > 0)) throw new Error("total notional must be positive");
}

export function buildGrid(config, referencePrice) {
  validateGridConfig(config);
  if (!(referencePrice > 0)) throw new Error("reference price must be positive");

  const spacing = (config.upperPrice - config.lowerPrice) / (config.levels - 1);
  const notionalPerLevel = config.totalNotionalUsd / config.levels;
  return Array.from({ length: config.levels }, (_, index) => {
    const price = round(config.lowerPrice + spacing * index, 6);
    const side = price < referencePrice ? "buy" : price > referencePrice ? "sell" : "skip";
    const size = round(notionalPerLevel / price, config.sizeDecimals);
    return { index, side, price, size, notionalUsd: round(size * price, 4) };
  });
}
