// Render the whole shell (App) in tests. Split from render.tsx so that only the files that render
// the shell pay for loading every routed page.
//
// Why the pages load here (W53 integration, root cause of the shell tests' timeouts under load):
// the route table loads each page with React.lazy, so the first render of a page inside a test
// imported and evaluated that page's module graph inside the test's own 5 s budget (about 1.7 s of
// module work for the whole table on an idle 4-core box, three to five times that when the box is
// busy). The eager glob below does that work while the test file is imported, outside any test
// timeout; the lazy import() in routes.ts then resolves from the module cache at once.

import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { App, type AppProps } from "../App.tsx";
import { LocationProbe, testClient } from "./render.tsx";

import.meta.glob(["../pages/*Page.tsx", "../shell/PendingPage.tsx"], { eager: true });

export function renderApp(path = "/", props: AppProps = {}) {
  return render(
    <QueryClientProvider client={testClient()}>
      <MemoryRouter initialEntries={[path]}>
        <App {...props} />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
