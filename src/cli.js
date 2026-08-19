import { loadConfig } from "./config.js";
import { buildGrid } from "./grid.js";
import { fetchHypeMarket, subscribeHype } from "./hyperliquid.js";

function printAttribution(config) {
  console.log("Entropy attribution (for future signed orders):");
  console.log(JSON.stringify({ b: config.builderAddress, f: config.builderFee }, null, 2));
}

async function main() {
  const config = loadConfig();
  const command = process.argv[2] || "probe";

  if (command === "probe") {
    const market = await fetchHypeMarket(config);
    console.log(JSON.stringify(market, null, 2));
    printAttribution(config);
    return;
  }

  if (command === "plan") {
    const market = await fetchHypeMarket(config);
    const price = market.hyperliquid.midPrice || market.hyperliquid.markPrice;
    console.log(`Reference HYPE price: ${price}`);
    console.table(buildGrid(config, price));
    printAttribution(config);
    console.log("DRY RUN: no orders were signed or submitted.");
    return;
  }

  if (command === "stream") {
    console.log("Streaming HYPE for 20 seconds...");
    const updates = await subscribeHype(config);
    console.log(`Received ${updates} active-asset updates.`);
    return;
  }

  throw new Error(`Unknown command: ${command}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
