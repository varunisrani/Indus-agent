// The dashboard's favicon (W63, the Indus AGI rebrand): the app tile of the brand artwork (the
// white mark on an ember square), written from @indus/ui's mark at build time instead of kept as
// an image file in this tree (the app ships no imagery of its own; src/shell/assets.test.ts). A
// build emits it as `dist/favicon.svg`, which the dashboard serves from the bundle root like any
// other top-level file, and the dev server answers `/favicon.svg` with the same document.

import { BRAND_COLORS, markSvg } from "@indus/ui";
import type { Plugin } from "vite";

export const FAVICON_PATH = "favicon.svg";
/** Room around the mark on the tile, in the mark's 100-unit box (the app icon's proportions). */
export const FAVICON_INSET = 30;

/** The favicon document: the white mark on a solid ember square. */
export function faviconSvg(): string {
  return markSvg(BRAND_COLORS.white, BRAND_COLORS.ember, FAVICON_INSET);
}

export function brandFavicon(): Plugin {
  return {
    name: "indus-brand-favicon",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url?.split("?")[0] !== `/${FAVICON_PATH}`) return next();
        res.setHeader("content-type", "image/svg+xml");
        res.end(faviconSvg());
      });
    },
    generateBundle() {
      this.emitFile({ type: "asset", fileName: FAVICON_PATH, source: faviconSvg() });
    },
  };
}
