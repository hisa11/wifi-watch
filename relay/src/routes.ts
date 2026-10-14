// 中継のAPIの一覧（M6で中身を足す）。
import type { Route } from "./handler.js";
import { makeHandler } from "./handler.js";

export const routes: Route[] = [];

export const handle = makeHandler(routes);
