import { useI18n } from "../i18n";

export function LoadingView({ label }: { label?: string }) {
  const { t } = useI18n();
  return (
    <div className="loading-view" role="status">
      <span className="loading-mark" aria-hidden="true" />
      <span>{label ?? t("common.loading")}</span>
    </div>
  );
}
