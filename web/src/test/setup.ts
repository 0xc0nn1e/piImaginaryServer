import "@testing-library/jest-dom/vitest";

// Vitest 4's jsdom environment does not expose Web Storage, and Node's own
// localStorage global is inert without --localstorage-file. Tests need real
// per-test storage, so install a minimal in-memory implementation.
function installLocalStorage() {
  if (typeof window !== "undefined" && window.localStorage) return;
  const entries = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return entries.size;
    },
    clear: () => entries.clear(),
    getItem: (key: string) => entries.get(String(key)) ?? null,
    key: (index: number) => [...entries.keys()][index] ?? null,
    removeItem: (key: string) => void entries.delete(String(key)),
    setItem: (key: string, value: string) => void entries.set(String(key), String(value)),
  };
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: storage,
  });
}

beforeEach(() => {
  installLocalStorage();
  window.localStorage.clear();
  window.localStorage.setItem("wave-archive-locale", "zh-HK");
});

afterEach(() => {
  vi.unstubAllGlobals();
});
