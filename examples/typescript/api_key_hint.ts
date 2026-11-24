/** Shared by the demos: the clickable page for making a URUN_API_KEY (it
 * also walks you through creating an account if you don't have one). */

export const apiKeysUrl = "https://console.urun.sh/settings/api-keys";

/** The message every demo prints when URUN_API_KEY is not set. */
export function missingApiKey(extra = ""): string {
  return `Set URUN_API_KEY — make one at ${apiKeysUrl}${extra ? `, or ${extra}` : ""}.`;
}
