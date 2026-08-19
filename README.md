# Entropy HYPE Grid

Read-only first-stage validator for an Entropy-attributed HYPE perpetual grid.

## Commands

```powershell
npm test
npm run probe
npm run plan
npm run stream
```

All current commands are read-only. They cannot sign or submit an order.

Configuration can be supplied with the environment variables documented in
`.env.example`. The current Entropy builder attribution is displayed with every
generated plan so it can later be included in signed Hyperliquid order actions.

Do not put a main-wallet private key in this project. The live phase will use a
separately approved API/Agent wallet and an explicit live-trading switch.
