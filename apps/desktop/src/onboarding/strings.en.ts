// English strings of the provider onboarding overlay (P20-33), spread into `src/i18n/en.ts`.

export const ONBOARDING_STRINGS = {
  "onboarding.title": "Choose how Indus reaches a model",
  "onboarding.lead":
    "Indus needs one model provider before it can answer. You can change it later.",
  "onboarding.checking": "Asking Indus which providers are set up",
  "onboarding.later": "Choose later",
  "onboarding.back": "Back",
  "onboarding.cancel": "Cancel",
  "onboarding.notConnected": "Indus is not connected; try again in a moment.",

  "onboarding.device.title": "Sign in with an account",
  "onboarding.device.starting": "Asking {provider} for a sign-in code",
  "onboarding.device.instructions":
    "Open the sign-in page of {provider} and enter this code. This window updates by itself.",
  "onboarding.device.code": "Sign-in code",
  "onboarding.device.copy": "Copy the code",
  "onboarding.device.copied": "Copied",
  "onboarding.device.open": "Open the sign-in page",
  "onboarding.device.waiting": "Waiting for you to approve the sign-in",

  "onboarding.other.title": "Other ways",
  "onboarding.key.option": "Paste an API key",
  "onboarding.key.optionHint": "For any provider that gives you a key.",
  "onboarding.key.provider": "Provider",
  "onboarding.key.generic": "Another key (type its variable name)",
  "onboarding.key.name": "Variable name",
  "onboarding.key.value": "API key",
  "onboarding.key.save": "Save the key",
  "onboarding.local.option": "Use a model running on this computer",
  "onboarding.local.optionHint": "A local runtime with an OpenAI-compatible server.",
  "onboarding.local.body": "The address of the local server. Indus asks it which models it has.",
  "onboarding.custom.option": "Use a custom OpenAI-compatible endpoint",
  "onboarding.custom.optionHint": "Any server that speaks the OpenAI API.",
  "onboarding.custom.body": "The endpoint's base URL and, if it needs one, its key.",
  "onboarding.endpoint.url": "Base URL",
  "onboarding.endpoint.key": "API key (optional)",
  "onboarding.endpoint.test": "Test and continue",

  "onboarding.model.body": "Pick the model Indus uses by default. You can switch any time.",
  "onboarding.model.label": "Model",
  "onboarding.model.save": "Start chatting",

  "onboarding.error.default": "The sign-in did not finish.",
  "onboarding.error.other": "Choose another way",
  "onboarding.error.useKey": "Paste an API key instead",
  "onboarding.error.retry": "Try again",
} as const satisfies Record<string, string>;
