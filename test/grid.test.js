import test from "node:test";
import assert from "node:assert/strict";
import { buildGrid, validateGridConfig } from "../src/grid.js";

const config = {
  lowerPrice: 30,
  upperPrice: 50,
  levels: 5,
  totalNotionalUsd: 100,
  sizeDecimals: 2
};

test("builds evenly spaced buy and sell levels", () => {
  const grid = buildGrid(config, 40);
  assert.deepEqual(grid.map((level) => level.price), [30, 35, 40, 45, 50]);
  assert.deepEqual(grid.map((level) => level.side), ["buy", "buy", "skip", "sell", "sell"]);
});

test("rounds order size to market size precision", () => {
  const grid = buildGrid(config, 41);
  assert.equal(grid[0].size, 0.67);
  assert.equal(grid[4].size, 0.4);
});

test("rejects invalid ranges", () => {
  assert.throws(() => validateGridConfig({ ...config, upperPrice: 20 }), /upper price/);
  assert.throws(() => validateGridConfig({ ...config, levels: 2 }), /levels/);
});
