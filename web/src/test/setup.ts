import "@testing-library/jest-dom/vitest";

beforeEach(() => {
  window.localStorage.setItem("wave-archive-locale", "zh-HK");
});

afterEach(() => {
  vi.unstubAllGlobals();
});
