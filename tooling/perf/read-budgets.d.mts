export type BudgetTable = Record<string, number | string>;
export type Budgets = Record<string, Record<string, BudgetTable> | BudgetTable>;
export declare const BUDGETS_PATH: string;
export declare function parseBudgets(text: string): Budgets;
export declare function readBudgets(path?: string): Budgets;
