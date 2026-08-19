async function postJson(url, body, timeoutMs = 10_000) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs)
  });
  if (!response.ok) throw new Error(`${url} returned HTTP ${response.status}`);
  return response.json();
}

export async function fetchHypeMarket(config) {
  const [metaAndCtxs, entropyResponse] = await Promise.all([
    postJson(config.infoUrl, { type: "metaAndAssetCtxs" }),
    fetch(config.entropyMarketsUrl, { signal: AbortSignal.timeout(10_000) })
  ]);
  if (!entropyResponse.ok) throw new Error(`Entropy markets returned HTTP ${entropyResponse.status}`);

  const entropyData = await entropyResponse.json();
  const entropyMarket = entropyData.markets?.find((market) => market.name === config.coin);
  const [meta, contexts] = metaAndCtxs;
  const index = meta.universe.findIndex((market) => market.name === config.coin);
  if (index < 0) throw new Error(`${config.coin} is absent from Hyperliquid metadata`);

  const hyperMarket = meta.universe[index];
  const context = contexts[index];
  return {
    entropy: entropyMarket,
    hyperliquid: {
      index,
      ...hyperMarket,
      markPrice: Number(context.markPx),
      oraclePrice: Number(context.oraclePx),
      midPrice: Number(context.midPx),
      funding: Number(context.funding),
      openInterest: Number(context.openInterest)
    }
  };
}

export function subscribeHype(config, { seconds = 20, onPrice = console.log } = {}) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(config.wsUrl);
    const timeout = setTimeout(() => socket.close(1000, "sample complete"), seconds * 1000);
    let updates = 0;

    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({
        method: "subscribe",
        subscription: { type: "activeAssetCtx", coin: config.coin }
      }));
    });
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.channel !== "activeAssetCtx") return;
      updates += 1;
      onPrice({
        time: new Date().toISOString(),
        coin: config.coin,
        markPrice: Number(message.data?.ctx?.markPx),
        oraclePrice: Number(message.data?.ctx?.oraclePx),
        funding: Number(message.data?.ctx?.funding)
      });
    });
    socket.addEventListener("error", () => {
      clearTimeout(timeout);
      reject(new Error("Hyperliquid WebSocket connection failed"));
    });
    socket.addEventListener("close", () => {
      clearTimeout(timeout);
      resolve(updates);
    });
  });
}
