# What the library contract tests cover

For every tool in `server.TOOLS`, every argument of every tool, and every layer
field the server reads from a store knowledge-store-builder v0.19.1 wrote: the
test that fails if that read breaks. Each row is a claim you can check. Break
the read it names and the cited test goes red.

`tests/test_library_coverage.py` holds this map to the code:

- every tool has a row citing a library contract test;
- every argument has a row citing a library contract test, or a fixture test
  and the reason library output cannot exercise the argument;
- every cited test resolves with `unittest.defaultTestLoader.loadTestsFromName`.

A new tool, a new argument or a renamed test therefore fails the suite until
this map says what covers it.

Each library contract runs in both read modes. `harness.both_modes` makes a
`…WithIndex` class over `store/` and a `…WithoutIndex` class over `no-index/`,
where the graph is read whole. The map cites the `WithIndex` form, and the guard
checks that its `WithoutIndex` twin exists.

Fixture tests (`test_tools_*.py`, over `tests/fixture_store/`) are cited only
where the library's output cannot produce the case.

## Tools and arguments

`—` in the Argument column is the tool's own row.

| Tool | Argument | Library contract test | Fixture test, when library output cannot exercise it |
|---|---|---|---|
| `store_info` | — | `test_library_graph.GraphWithIndex.test_store_info_reports_the_library_store` | |
| `store_info` | `cursor` | `test_library_graph.GraphWithIndex.test_store_info_pages_the_provenance_by_its_cursor` | |
| `find_repo` | — | `test_library_graph.GraphWithIndex.test_find_repo_knows_each_clone`, `test_library_graph.GraphWithIndex.test_find_repo_answers_a_target_outside_the_estate` | |
| `find_repo` | `name` | `test_library_graph.GraphWithIndex.test_find_repo_knows_each_clone`, `test_library_graph.GraphWithIndex.test_find_repo_answers_a_target_outside_the_estate`, `test_library_store.RelationshipsWithIndex.test_only_by_repo_files_are_repositories` | |
| `search` | — | `test_library_graph.GraphWithIndex.test_search_finds_a_class_with_its_file_and_link` | |
| `search` | `text` | `test_library_graph.GraphWithIndex.test_search_finds_a_class_with_its_file_and_link` | |
| `search` | `repo` | `test_library_graph.GraphWithIndex.test_search_scoped_to_a_repository` | |
| `search` | `kind` | `test_library_graph.GraphWithIndex.test_kind_filters_to_the_json_schema_kinds` | |
| `search` | `limit` | `test_library_graph.GraphWithIndex.test_search_limit_keeps_the_best_ranked_and_counts_every_match` | |
| `search` | `cursor` | `test_library_graph.GraphWithIndex.test_search_pages_its_ranked_nodes_by_its_cursor` | |
| `node` | — | `test_library_graph.GraphWithIndex.test_node_by_its_merged_id` | |
| `node` | `id` | `test_library_graph.GraphWithIndex.test_node_by_its_merged_id` | |
| `neighbours` | — | `test_library_graph.GraphWithIndex.test_neighbours_follow_a_calls_edge` | |
| `neighbours` | `id` | `test_library_graph.GraphWithIndex.test_neighbours_follow_a_calls_edge` | |
| `neighbours` | `depth` | `test_library_graph.GraphWithIndex.test_neighbours_at_depth_two_reach_the_schema_s_other_parts` | |
| `neighbours` | `relation` | `test_library_graph.GraphWithIndex.test_neighbours_follow_a_calls_edge`, `test_library_graph.GraphWithIndex.test_a_schema_contains_its_definition_and_fields`, `test_library_graph.GraphWithIndex.test_a_definition_extends_the_definition_its_all_of_names` | |
| `neighbours` | `cursor` | `test_library_graph.GraphWithIndex.test_neighbours_page_by_their_cursor` | |
| `community` | — | `test_library_graph.GraphWithIndex.test_community_has_its_label_members_and_summary`, `test_library_graph.GraphWithIndex.test_a_community_without_a_summary` | |
| `community` | `id` | `test_library_graph.GraphWithIndex.test_community_has_its_label_members_and_summary` | |
| `community` | `cursor` | `test_library_graph.GraphWithIndex.test_a_community_pages_its_members_by_its_cursor` | |
| `repo_answer` | — | `test_library_store.RelationshipsWithIndex.test_a_repository_with_no_edges_is_in_the_estate_and_answered_empty` | |
| `repo_answer` | `repo` | `test_library_store.RelationshipsWithIndex.test_dependents_and_dependencies_return_the_library_edge`, `test_library_store.RelationshipsWithIndex.test_a_repository_with_no_edges_is_in_the_estate_and_answered_empty` | |
| `repo_answer` | `cursor` | `test_library_store.RelationshipsWithIndex.test_repo_answer_over_the_cap_pages_the_library_file` (beta-app's three edges over three pages, across both lists, and a refused foreign cursor) | |
| `dependents` | — | `test_library_store.RelationshipsWithIndex.test_dependents_and_dependencies_return_the_library_edge` | |
| `dependents` | `target` | `test_library_store.RelationshipsWithIndex.test_dependents_and_dependencies_return_the_library_edge` | |
| `dependents` | `kind` | `test_library_store.RelationshipsWithIndex.test_a_kind_keeps_the_edges_and_targets_of_that_kind`, `test_library_store.RelationshipsWithIndex.test_a_kind_the_library_does_not_list_is_refused_with_its_kinds` | |
| `dependents` | `ref_prefix_not` | `test_library_store.RelationshipsWithIndex.test_ref_prefix_not_keeps_a_consumer_by_the_refs_the_library_pinned` | |
| `dependents` | `transitive` | `test_library_store.RelationshipsWithIndex.test_dependents_and_dependencies_return_the_library_edge`, `test_library_store.RelationshipsWithIndex.test_a_transitive_walk_reports_the_loop_once_and_never_follows_it` | |
| `dependents` | `max_depth` | `test_library_store.RelationshipsWithIndex.test_max_depth_bounds_the_walk_and_names_what_lies_beyond` (one hop cuts eta-portal and names beta-app beyond; two hops cut nothing) | |
| `dependents` | `cursor` | `test_library_store.RelationshipsWithIndex.test_dependents_take_only_their_own_cursor` (beta-app's two consumers over two pages, and a refused foreign cursor) | |
| `dependencies` | — | `test_library_store.RelationshipsWithIndex.test_dependents_and_dependencies_return_the_library_edge` | |
| `dependencies` | `repo` | `test_library_store.RelationshipsWithIndex.test_dependents_and_dependencies_return_the_library_edge` | |
| `dependencies` | `kind` | `test_library_store.RelationshipsWithIndex.test_a_kind_keeps_the_edges_and_targets_of_that_kind`, `test_library_store.RelationshipsWithIndex.test_a_kind_the_library_does_not_list_is_refused_with_its_kinds` | |
| `dependencies` | `cursor` | `test_library_store.RelationshipsWithIndex.test_dependencies_page_the_edge_and_the_target_outside_by_their_cursor` | |
| `tickets` | — | `test_library_tickets.TicketsWithIndex.test_tickets_for_a_file_list_the_landed_ticket` | |
| `tickets` | `repo` | `test_library_tickets.TicketsWithIndex.test_tickets_for_a_repository_are_every_file_s_tickets_there` | |
| `tickets` | `path` | `test_library_tickets.TicketsWithIndex.test_tickets_for_a_file_list_the_landed_ticket`, `test_library_tickets.TicketsWithIndex.test_tickets_keep_a_branch_only_ticket_apart_with_its_branch` | |
| `tickets` | `cursor` | `test_library_tickets.TicketsWithIndex.test_tickets_page_both_lists_by_their_cursor` | |
| `ticket` | — | `test_library_tickets.TicketsWithIndex.test_ticket_has_its_files_record_and_tracker_record` | |
| `ticket` | `key` | `test_library_tickets.TicketsWithIndex.test_ticket_has_its_files_record_and_tracker_record`, `test_library_tickets.TicketsWithIndex.test_a_key_no_layer_holds` | |
| `ticket` | `cursor` | `test_library_tickets.TicketsWithIndex.test_ticket_pages_its_files_by_its_cursor` | |
| `answers` | — | `test_library_tickets.TicketsWithIndex.test_answers_from_every_library_answer_layer` | |
| `answers` | `query` | `test_library_tickets.TicketsWithIndex.test_answers_from_every_library_answer_layer`, `test_library_tickets.TicketsWithIndex.test_an_answer_query_nothing_matches` | |
| `answers` | `limit` | `test_library_tickets.TicketsWithIndex.test_answers_limit_keeps_the_first_by_id_and_counts_every_match` | |
| `answers` | `cursor` | `test_library_tickets.TicketsWithIndex.test_answers_page_by_their_cursor` | |
| `minimum_standard` | — | `test_library_minimum_standard.MinimumStandardWithIndex.test_the_whole_layer_is_each_repository_s_record` | |
| `minimum_standard` | `repos` | `test_library_minimum_standard.MinimumStandardWithIndex.test_repos_scopes_to_the_names_given_as_a_list_or_as_text` | |
| `minimum_standard` | `pattern` | `test_library_minimum_standard.MinimumStandardWithIndex.test_a_pattern_is_a_glob_over_the_names_the_layer_holds` | |
| `minimum_standard` | `product` | `test_library_minimum_standard.MinimumStandardWithIndex.test_a_product_is_the_repositories_the_summary_declares_for_it`, `test_library_minimum_standard.MinimumStandardWithIndex.test_two_scopes_at_once_are_refused` | |
| `minimum_standard` | `cursor` | `test_library_minimum_standard.MinimumStandardWithIndex.test_the_layer_pages_one_repository_a_page_by_its_cursor` | |

Each library store test pages at a cap of one byte (`harness.pages`), so every
page holds one item. This gives a hand-derived page count.

A gap the estate cannot yet reach is closed by adding a generator input and
regenerating, not by a fixture test. None is left: the estate's loop
(alpha-lib <-> beta-app), beta-app's second consumer (eta-portal) and the
schema's `allOf` were added for the paging, depth, cycle and `extends` rows.

## Layer fields

| Layer field | Library contract test | Note |
|---|---|---|
| manifest `library_version`, `format` | `test_library_generation.LibraryGenerationTest.test_manifest_is_the_pinned_release_and_format`, `test_library_graph.GraphWithIndex.test_store_info_reports_the_library_store` | |
| manifest `layers.*.present`, every v0.19.1 layer (includes `semantic`, `explorer`, `context`, `content_files`, `history`, `repository_manifest`, `deployments`, which the server reads for presence only) | `test_library_generation.LibraryGenerationTest.test_every_library_layer_is_present`, `test_library_graph.GraphWithIndex.test_store_info_reports_the_library_store` | |
| manifest `sources_as_of` (`stale_days`, the date of summaries, briefs and dives) | `test_library_graph.GraphWithIndex.test_store_info_reports_the_library_store`, `test_library_graph.GraphWithIndex.test_community_has_its_label_members_and_summary`, `test_library_tickets.TicketsWithIndex.test_answers_from_every_library_answer_layer` | |
| manifest flags `graph.source_urls`, `intent.reach`, `deployments.present` | `test_library_graph.GraphWithIndex.test_store_info_reports_the_library_store`, `test_library_tickets.TicketsWithIndex.test_tickets_for_a_file_list_the_landed_ticket` | |
| manifest extra layers (`answers`, `ownership_notes`) | `test_library_graph.GraphWithIndex.test_store_info_reports_the_library_store`, `test_library_store.RelationshipsWithIndex.test_an_extra_layer_in_the_layer_directory_is_read_as_its_own_layer` | |
| manifest `layers.*.digest` and `extra_layers.*.digest` of a single-file layer, against the file's sha256 (`store_info` `digest_mismatch`) | `test_library_digests.DigestWithIndex.test_the_library_store_matches_its_manifest`, `test_library_digests.DigestWithIndex.test_a_changed_layer_file_is_named_and_no_other`, `test_library_graph.GraphWithIndex.test_store_info_reports_the_library_store` | run on a copy of the store, one file changed after the library wrote the manifest |
| a declared single-file layer not on disk or not readable as a file (`store_info` `digest_missing`), and layer names only, never a path | `test_library_digests.DigestWithIndex.test_a_deleted_layer_file_is_missing_not_mismatched`, `test_library_digests.DigestWithIndex.test_a_layer_file_replaced_by_a_directory_is_missing`, `test_library_digests.DigestWithIndex.test_the_fields_name_layers_and_never_a_path` | |
| directory layers `history`, `minimum_standard`, `relationships` named unchecked (`store_info` `digest_unchecked`) | `test_library_digests.DigestWithIndex.test_the_library_store_matches_its_manifest` | their digest covers the files a git clone receives, which the server cannot reproduce |
| digests hashed on a store's first `store_info`, never at open, once per loaded store and again on reload; the index archive's taken from the open handle | `test_library_digests.DigestWithIndex.test_digests_are_computed_once_per_loaded_store_and_again_on_reload`, `test_library_digests.DigestIndexArchive.test_a_changed_index_archive_is_named` | the archive test runs over the indexed store only (`DigestIndexArchive`, no twin): the store without the index records no archive |
| manifest `layers.relationships.paths` | `test_library_store.RelationshipsWithIndex.test_the_store_is_the_library_release_and_layout_it_claims` | |
| index `meta` schema | `test_library_graph.GraphWithIndex.test_store_info_reports_the_library_store` | `WithIndex` only, by design |
| provenance `sha`, `committed` | `test_library_graph.GraphWithIndex.test_store_info_reports_the_library_store`, `test_library_graph.GraphWithIndex.test_store_info_pages_the_provenance_by_its_cursor` | |
| index `repos` / graph repositories | `test_library_graph.GraphWithIndex.test_find_repo_knows_each_clone` | |
| graph and index node `id` (merge-graphs' `<repo>::` prefix), `label`, `repo`, `file` | `test_library_graph.GraphWithIndex.test_node_by_its_merged_id`, `test_library_graph.GraphWithIndex.test_search_finds_a_class_with_its_file_and_link` | |
| graphify's node for a top-level file: the bare-stem id (`alpha-lib::index`), labelled with its file name, and the function it `contains` | `test_library_graph.GraphWithIndex.test_a_root_file_is_served_by_its_bare_stem` | |
| node `url` (`source_urls`, pinned to the built sha) | `test_library_graph.GraphWithIndex.test_search_finds_a_class_with_its_file_and_link` | |
| node `community` | `test_library_generation.LibraryGenerationTest.test_every_node_has_a_community`, `test_library_graph.GraphWithIndex.test_community_has_its_label_members_and_summary` | |
| node `metadata.kind` (json-schema `schema`, `definition`, `field`) | `test_library_graph.GraphWithIndex.test_kind_filters_to_the_json_schema_kinds` | |
| index label FTS / graph label search, ranking by bm25 then id | `test_library_graph.GraphWithIndex.test_search_limit_keeps_the_best_ranked_and_counts_every_match` | |
| edges: relation and direction (graphify `calls`) | `test_library_graph.GraphWithIndex.test_neighbours_follow_a_calls_edge`, `test_library_graph.GraphWithIndex.test_neighbours_at_depth_two_reach_the_schema_s_other_parts` | |
| json-schema `contains` and `references` edges | `test_library_graph.GraphWithIndex.test_a_schema_contains_its_definition_and_fields` | |
| json-schema `extends` edges | `test_library_graph.GraphWithIndex.test_a_definition_extends_the_definition_its_all_of_names` | `credit`'s `allOf` names `money`, so v0.19.1 writes `credit` -> `money` as `extends` |
| `packages` `provided_by` edge: package node -> provider's `package.json` node | `test_library_graph.GraphWithIndex.test_a_package_is_provided_by_its_manifest` | v0.19.1 (#510) anchors it on the manifest's own node, never a `.json` schema node |
| graphify's keyword nodes for a schema file removed by the json-schema stage | `test_library_graph.GraphWithIndex.test_no_graphify_keyword_node_remains_for_the_schema` | |
| `labels` keyed by community | `test_library_graph.GraphWithIndex.test_community_has_its_label_members_and_summary`, `test_library_graph.GraphWithIndex.test_a_community_without_a_summary` | |
| `summaries` keyed by community | `test_library_graph.GraphWithIndex.test_community_has_its_label_members_and_summary`, `test_library_graph.GraphWithIndex.test_a_community_without_a_summary` | |
| relationships by-repo `depends_on`, `depended_on_by`, `commit`; edge `kinds`, `references`, `refs_pinned`, `refs_unpinned`, `examples` | `test_library_store.RelationshipsWithIndex.test_dependents_and_dependencies_return_the_library_edge`, `test_library_store.RelationshipsWithIndex.test_a_repository_with_no_edges_is_in_the_estate_and_answered_empty`, `test_library_store.RelationshipsWithIndex.test_repo_answer_over_the_cap_pages_the_library_file` | |
| `dependents(transitive=True)` `cycles`, over the library's `depended_on_by` edges | `test_library_store.RelationshipsWithIndex.test_a_transitive_walk_reports_the_loop_once_and_never_follows_it`, `test_library_store.RelationshipsWithIndex.test_max_depth_bounds_the_walk_and_names_what_lies_beyond` | alpha-lib and beta-app depend on each other |
| relationships `kinds.json` | `test_library_store.RelationshipsWithIndex.test_a_kind_the_library_does_not_list_is_refused_with_its_kinds` | |
| registry `unresolved_targets_outside_estate` | `test_library_graph.GraphWithIndex.test_find_repo_answers_a_target_outside_the_estate`, `test_library_store.RelationshipsWithIndex.test_ref_prefix_not_keeps_a_consumer_by_the_refs_the_library_pinned` | |
| only by-repo files are repositories | `test_library_store.RelationshipsWithIndex.test_only_by_repo_files_are_repositories` | |
| `intent` per-file tickets and `proposed`, commit counts; index `landed` | `test_library_tickets.TicketsWithIndex.test_tickets_for_a_file_list_the_landed_ticket`, `test_library_tickets.TicketsWithIndex.test_tickets_keep_a_branch_only_ticket_apart_with_its_branch`, `test_library_tickets.TicketsWithIndex.test_ticket_that_never_landed` | |
| `intent` / index: every file one commit landed (DEMO-7: `index.ts` and `package.json`) | `test_library_tickets.TicketsWithIndex.test_a_ticket_lists_every_file_its_commit_landed` | |
| `ticket_descriptions` `d`, `s`, `first`, `last`, `repos`, `n`, `proposed`, `branches` | `test_library_tickets.TicketsWithIndex.test_ticket_has_its_files_record_and_tracker_record`, `test_library_tickets.TicketsWithIndex.test_ticket_that_never_landed` | |
| `ticket_tracker` `fetched` (resolved and unresolved), `absent`, `denied`; `checked`; text fields; `comments_truncated` | `test_library_tickets.TicketsWithIndex.test_ticket_has_its_files_record_and_tracker_record`, `test_library_tickets.TicketsWithIndex.test_ticket_an_unresolved_tracker_record`, `test_library_tickets.TicketsWithIndex.test_ticket_the_tracker_records_absent`, `test_library_tickets.TicketsWithIndex.test_ticket_the_tracker_denied` | |
| `topics`, `deep_dives` (`title`, `html`, `source`; `_metadata` skipped) | `test_library_tickets.TicketsWithIndex.test_answers_from_every_library_answer_layer`, `test_library_tickets.TicketsWithIndex.test_an_answer_query_nothing_matches` | |
| `answers` extra layer `as_of`, `commit` | `test_library_tickets.TicketsWithIndex.test_answers_from_every_library_answer_layer` | |
| answer `html`: a fenced code block and a `mermaid` fence, as the library renders them | `test_library_tickets.TicketsWithIndex.test_an_answer_keeps_its_fenced_code_and_mermaid_diagram_intact` | |
| `ownership_notes` extra layer in the relationships directory | `test_library_tickets.TicketsWithIndex.test_answers_from_the_extra_layer_in_the_relationships_directory` | |
| `minimum_standard` records: each point's state, evidence and reason; v0.19.1 hygiene items | `test_library_minimum_standard.MinimumStandardWithIndex.test_the_whole_layer_is_each_repository_s_record`, `test_library_minimum_standard.MinimumStandardWithIndex.test_an_unprotected_branch_is_cited_under_hygiene_and_keeps_it_from_present`, `test_library_minimum_standard.MinimumStandardWithIndex.test_an_unreadable_setting_is_not_determinable_and_names_the_setting`, `test_library_minimum_standard.MinimumStandardWithIndex.test_a_live_repository_is_not_judged_unmaintained_and_cites_its_last_commit` | |
| `minimum_standard` secret findings (key file, key name; never a value) | `test_library_minimum_standard.MinimumStandardWithIndex.test_a_secret_finding_is_where_and_which_rule_never_the_value` | v0.19.1 writes no value. `test_tools_minimum_standard.EvidenceNotVerdictTest.test_a_secret_finding_is_a_rule_a_path_and_a_line_and_never_a_value` covers the filter that drops one |
| `minimum_standard` summary `products`, `notes` | `test_library_minimum_standard.MinimumStandardWithIndex.test_a_product_is_the_repositories_the_summary_declares_for_it`, `test_library_minimum_standard.MinimumStandardWithIndex.test_the_whole_layer_is_each_repository_s_record` | |

## Fixture only: cases the library does not write

The library never writes these cases, so only the synthetic fixture covers them.

| Case | Fixture test | Why library output cannot produce it |
|---|---|---|
| a secret value inside a finding | `test_tools_minimum_standard.EvidenceNotVerdictTest.test_a_secret_finding_is_a_rule_a_path_and_a_line_and_never_a_value` | v0.19.1 records where a finding is and which rule it broke, never the value |
| a repository depending on itself | `test_tools_relationships.DependentsTest.test_a_repository_depending_on_itself_is_a_cycle` | the library's relationships stage treats a self-reference as a self-reference, not an edge, and places it nowhere (`relationships.py` `_Resolver`, `_check_conservation`). It never writes a repository among its own dependents. |
| a by-repo link escaping the store | `test_tools_relationships.EscapingLinkTest.test_a_by_repo_link_outside_the_store_is_skipped_and_counted` | the library writes files, not symlinks |
| a damaged full-text table | `test_tools_graph.DamagedIndexTest.test_a_damaged_full_text_table_falls_back_to_the_graph_and_says_why` | the library writes a sound index |

## Older stores

Real stores lag the library, so the server must keep reading stores that older
releases wrote. `frozen/v0.18.3/` (manifest format 1.3, before `json-schema` and
`minimum_standard`) and `frozen/v0.19.0/` hold stores built from the same inputs
by those releases (`generate.py --release`). `test_library_older_stores` serves
each one over the wire, with its index and without it. It makes four classes,
`V0_18_3Indexed`, `V0_18_3Unindexed`, `V0_19_0Indexed` and `V0_19_0Unindexed`.
The rows cite the `V0_18_3Indexed` form.

| Claim | Test |
|---|---|
| the frozen manifest is the release's own: version, format and which layers it records | `test_library_older_stores.V0_18_3Indexed.test_the_manifest_records_the_release_and_its_layers` |
| every tool answers without `isError`, and a new tool fails until it is called here | `test_library_older_stores.V0_18_3Indexed.test_every_tool_answers_without_an_error` |
| `dependents` and `ticket` answer with what the inputs put there, not just without an error | `test_library_older_stores.V0_18_3Indexed.test_dependents_and_a_ticket_answer_from_the_inputs` |
| `store_info` reports no `unread` layer, and a layer the release never wrote is left out | `test_library_older_stores.V0_18_3Indexed.test_store_info_reads_every_layer_and_leaves_none_unread` |
| `minimum_standard` reads absent where the release has no layer, and answers where it has one | `test_library_older_stores.V0_18_3Indexed.test_minimum_standard_reads_absent_or_answers` |
| `provided_by` is served out to exactly the node the release anchored it on: alpha-lib's lowest node, `index`, in both releases, where v0.19.1 takes the manifest's | `test_library_older_stores.V0_18_3Indexed.test_a_package_is_provided_by_the_node_its_release_anchored` |
| JSON Schema nodes are served where the release wrote them, and an older store answers "no such node" without an error | `test_library_older_stores.V0_18_3Indexed.test_json_schema_nodes_are_served_where_the_release_wrote_them` |

alpha-lib's root `index.ts` gives it a node, `index`, whose id sorts below the
manifest's `package`. So v0.18.3 and v0.19.0 anchor `provided_by` on
`alpha-lib::index` and v0.19.1 on `alpha-lib::package`, and the `provided_by` row
catches a read that follows only v0.19.1's anchor as well as one that drops or
reverses the edge.

## Partial store

Real stores are rarely whole. `partial/store/` and `partial/no-index/` are the
main pin over the same estate with `generate.PARTIAL_SKIP`'s stages not run
(`minimum-standard-settings`, `minimum-standard`, `topics`, `deepdive`,
`deployments`) and the history kept off the store's git by a root `.gitignore`.
`test_library_partial` serves both, as `PartialContractIndexed` and
`PartialContractUnindexed`. The rows cite the `Indexed` form.

| Claim | Test |
|---|---|
| the library writes each unbuilt layer in its own shape: `minimum_standard` `present: false` with `recognised: null`, `topics` and `deep_dives` `present: false`, `deployments` `present: null`, `history` `local_only` | `test_library_partial.PartialContractIndexed.test_the_manifest_writes_each_unbuilt_layer_in_the_library_s_shape` |
| `store_info` reports each as recorded, a `null` as `null`, and none `unread` | `test_library_partial.PartialContractIndexed.test_store_info_reports_each_unbuilt_layer_absent_and_none_unread` |
| `minimum_standard` answers that the layer is absent | `test_library_partial.PartialContractIndexed.test_minimum_standard_answers_that_the_layer_is_absent` |
| precondition, read from the committed artefacts: the index lists each `RETIRED` repository with the files its commits landed and no node, and neither graph holds one | `test_library_generation.PartialStoreInputTest.test_the_retired_repository_has_ticket_rows_and_no_node` |
| `answers` reads the written answers alone when the briefs and dives were never built | `test_library_partial.PartialContractIndexed.test_answers_come_from_the_written_answers_alone` |
| a retired repository only the ticket layer and the index name (`zeta-archive`) is "in the estate" for `find_repo` and `tickets` in both modes, with its `DEMO-2` row; a scoped search finds no node where alpha-lib's finds one, its would-be node id is not served, and it has no provenance record (DESIGN.md 6.4; the root cause, `intent` indexing a history directory the estate no longer names, is upstream) | `test_library_partial.PartialContractIndexed.test_a_retired_repository_the_ticket_layer_names_is_in_the_estate` |
