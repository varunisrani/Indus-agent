// Settings › Plugins (W52-j): the desktop plugin inventory (P24-32) on the page frame.

import { PluginsPage } from "../../contrib/PluginsPage.tsx";
import { PageBody } from "../primitives.tsx";

export function PluginsSettingsPage() {
  return (
    <PageBody>
      <div className="sb-plugins">
        <PluginsPage />
      </div>
    </PageBody>
  );
}
