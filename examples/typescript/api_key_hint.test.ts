/** Tests for the shared missing-key message (mirrors
 * python/tests/test_cheese_demo.py's missing-key check). */
import { describe, expect, test } from "bun:test";
import { apiKeysUrl, missingApiKey } from "./api_key_hint";

describe("missingApiKey", () => {
  test("links the api keys page (which also prompts account creation)", () => {
    expect(missingApiKey()).toContain("https://console.urun.sh/settings/api-keys");
  });

  test("keeps the demo's alternative when one is given", () => {
    expect(missingApiKey("run --mic-only")).toBe(
      `Set URUN_API_KEY — make one at ${apiKeysUrl}, or run --mic-only.`,
    );
  });
});
