import { Fragment } from "react";

import type { FuriganaMap } from "../types";

interface FuriganaProps {
  text: string;
  readings: FuriganaMap | undefined;
}

/**
 * Render Japanese text with hiragana above its kanji.
 *
 * Built from React nodes rather than markup so model-produced text is never
 * interpreted as HTML. When the server sent no reading for this string — older
 * data, or text with no kanji — the plain text renders unchanged.
 */
export function Furigana({ text, readings }: FuriganaProps) {
  // The map is JSON-parsed, so it inherits Object.prototype: text that happens
  // to equal a member name such as "constructor" or "__proto__" would otherwise
  // resolve to that member and blow up the whole page on render. Requiring an
  // array also absorbs any malformed payload.
  const tokens = readings?.[text];
  if (!Array.isArray(tokens) || tokens.length === 0) return <>{text}</>;

  return (
    <>
      {tokens.map((token, index) => {
        if (!token.reading) return <Fragment key={index}>{token.text}</Fragment>;
        return (
          <ruby key={index}>
            {token.text}
            {/* rp text keeps the reading legible if ruby is unsupported */}
            <rp>(</rp>
            <rt>{token.reading}</rt>
            <rp>)</rp>
          </ruby>
        );
      })}
    </>
  );
}
