import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Furigana } from "../components/Furigana";
import type { FuriganaMap } from "../types";

const quote = "一旦こちらで持ち帰ります。";
const readings: FuriganaMap = {
  [quote]: [
    { text: "一旦", reading: "いったん" },
    { text: "こちらで", reading: null },
    { text: "持ち帰", reading: "もちかえ" },
    { text: "ります。", reading: null },
  ],
};

describe("furigana rendering", () => {
  it("sets each reading over its own kanji run", () => {
    const { container } = render(<Furigana text={quote} readings={readings} />);

    const rubies = Array.from(container.querySelectorAll("ruby"));
    expect(rubies.map((ruby) => ruby.querySelector("rt")?.textContent)).toEqual([
      "いったん",
      "もちかえ",
    ]);
    // The visible sentence must read exactly as stored, ignoring the readings.
    const withoutReadings = Array.from(container.querySelectorAll("rt, rp")).reduce(
      (text, node) => text.replace(node.textContent ?? "", ""),
      container.textContent ?? "",
    );
    expect(withoutReadings).toContain("持ち帰ります。");
  });

  it("renders plain text when the server sent no reading for it", () => {
    const { container } = render(<Furigana text="はい、そうですね。" readings={readings} />);

    expect(container.querySelectorAll("ruby")).toHaveLength(0);
    expect(screen.getByText("はい、そうですね。")).toBeInTheDocument();
  });

  it("renders plain text when the map is missing entirely", () => {
    // Older responses and cached pages carry no map; the sentence must survive.
    const { container } = render(<Furigana text={quote} readings={undefined} />);

    expect(container.querySelectorAll("ruby")).toHaveLength(0);
    expect(container.textContent).toBe(quote);
  });

  it.each(["constructor", "__proto__", "hasOwnProperty", "toString", "valueOf"])(
    "renders %s as ordinary text instead of crashing",
    (inherited) => {
      // The map arrives from JSON.parse, so it inherits Object.prototype. Text
      // equal to a member name must not resolve to that member; before the
      // Array guard this threw and unmounted the whole page.
      const parsed = JSON.parse('{"漢字":[{"text":"漢字","reading":"かんじ"}]}') as FuriganaMap;

      const { container } = render(<Furigana text={inherited} readings={parsed} />);

      expect(container.textContent).toBe(inherited);
      expect(container.querySelectorAll("ruby")).toHaveLength(0);
    },
  );

  it("never interprets the annotated text as markup", () => {
    const hostile = "<img src=x onerror=alert(1)>";
    const { container } = render(
      <Furigana
        text={hostile}
        readings={{ [hostile]: [{ text: hostile, reading: "よみ" }] }}
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain(hostile);
  });
});
