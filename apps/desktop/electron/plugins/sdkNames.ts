// The named exports of the two host shim modules a desktop plugin may import (P24-32). Main
// generates `indus-plugin://host/sdk.js` and `react.js` from these lists; renderer tests prove the
// lists match what `src/contrib/sdk.ts` and React really export.

/** `import { … } from "@indus/desktop-sdk"`. */
export const SDK_EXPORTS: readonly string[] = ["AREAS", "definePlugin", "ui", "version"];

/** `import { … } from "react"` (React 19). */
export const REACT_EXPORTS: readonly string[] = [
  "Children",
  "Component",
  "Fragment",
  "Profiler",
  "PureComponent",
  "StrictMode",
  "Suspense",
  "cloneElement",
  "createContext",
  "createElement",
  "createRef",
  "forwardRef",
  "isValidElement",
  "lazy",
  "memo",
  "startTransition",
  "use",
  "useActionState",
  "useCallback",
  "useContext",
  "useDebugValue",
  "useDeferredValue",
  "useEffect",
  "useId",
  "useImperativeHandle",
  "useInsertionEffect",
  "useLayoutEffect",
  "useMemo",
  "useOptimistic",
  "useReducer",
  "useRef",
  "useState",
  "useSyncExternalStore",
  "useTransition",
  "version",
];
