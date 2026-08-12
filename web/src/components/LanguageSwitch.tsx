import { useI18n } from "../i18n";

export function LanguageSwitch({ compact = false }: { compact?: boolean }) {
  const { locale, setLocale, t } = useI18n();

  return (
    <div className={`language-switch${compact ? " language-switch-compact" : ""}`} aria-label={t("language.label")} role="group">
      <button
        aria-label={t("language.ja")}
        aria-pressed={locale === "ja"}
        className={locale === "ja" ? "active" : ""}
        lang="ja"
        type="button"
        onClick={() => setLocale("ja")}
      >
        {compact ? "JP" : t("language.ja")}
      </button>
      <button
        aria-label={t("language.hk")}
        aria-pressed={locale === "zh-HK"}
        className={locale === "zh-HK" ? "active" : ""}
        lang="zh-HK"
        type="button"
        onClick={() => setLocale("zh-HK")}
      >
        {compact ? "HK" : t("language.hk")}
      </button>
    </div>
  );
}
