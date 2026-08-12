import { statusTone } from "../format";
import { statusLabelKey, useI18n } from "../i18n";
import type { RecordingStatus } from "../types";

export function StatusBadge({ status }: { status: RecordingStatus }) {
  const { t } = useI18n();
  return (
    <span className={`status-badge status-${statusTone(status)}`}>
      <span aria-hidden="true" className="status-dot" />
      {t(statusLabelKey(status))}
    </span>
  );
}
