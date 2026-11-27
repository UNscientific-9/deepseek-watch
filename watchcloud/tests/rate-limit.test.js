import test from "node:test";
import assert from "node:assert/strict";

import { rateLimit } from "../src/rate-limit.js";

function requestFrom(ip) {
  return {
    headers: {
      get(name) {
        return name.toLowerCase() === "cf-connecting-ip" ? ip : "";
      },
    },
  };
}

test("ingest bucket allows 30 requests then rejects the 31st", () => {
  const ip = `test-ingest-${Date.now()}`;
  for (let i = 0; i < 30; i += 1) {
    assert.equal(rateLimit(requestFrom(ip), "ingest").limited, false);
  }
  const blocked = rateLimit(requestFrom(ip), "ingest");
  assert.equal(blocked.limited, true);
  assert.ok(blocked.retryAfter >= 1);
});

test("different IP buckets do not share counters", () => {
  const a = `test-a-${Date.now()}`;
  const b = `test-b-${Date.now()}`;
  for (let i = 0; i < 30; i += 1) rateLimit(requestFrom(a), "ingest");
  assert.equal(rateLimit(requestFrom(a), "ingest").limited, true);
  assert.equal(rateLimit(requestFrom(b), "ingest").limited, false);
});
