// Async iterator helper for endpoints that return next_page_token-paginated
// responses (the standard pattern across the Coval v1 API).
//
// Generic enough that callers can use it with any list method:
//
//   for await (const agent of paginate({
//     fetchPage: (token) => agents.listAgents({ pageToken: token }),
//     items: (page) => page.agents,
//     nextToken: (page) => page.next_page_token,
//   })) {
//     console.log(agent.display_name);
//   }

export interface PaginateOptions<TPage, TItem> {
  fetchPage: (pageToken: string | undefined) => Promise<TPage>;
  items: (page: TPage) => TItem[] | undefined | null;
  nextToken: (page: TPage) => string | undefined | null;
  maxPages?: number;
}

export async function* paginate<TPage, TItem>(
  opts: PaginateOptions<TPage, TItem>,
): AsyncGenerator<TItem, void, void> {
  let token: string | undefined = undefined;
  let pageCount = 0;
  const cap = opts.maxPages ?? Infinity;

  while (true) {
    const page = await opts.fetchPage(token);
    pageCount += 1;
    const batch = opts.items(page) ?? [];
    for (const item of batch) yield item;

    const next = opts.nextToken(page);
    if (!next || pageCount >= cap) return;
    token = next;
  }
}

export async function collectAll<TPage, TItem>(
  opts: PaginateOptions<TPage, TItem>,
): Promise<TItem[]> {
  const out: TItem[] = [];
  for await (const item of paginate(opts)) out.push(item);
  return out;
}
