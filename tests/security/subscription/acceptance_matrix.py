"""The required negative-test matrix, mapped to the tests that prove each row.

Each row of the plan's negative-test matrix lists the existing tests that
exercise it. ``test_acceptance`` fails when a row has no evidence or names a test
that no longer exists, so a row cannot silently lose its proof. Evidence ids are
``<path relative to tests/>::<test function>``.
"""

from __future__ import annotations

U = "unit/subscription"
S = "security/subscription"
I = "integration/subscription"  # noqa: E741 - mirrors the directory name

MATRIX: dict[str, tuple[str, ...]] = {
    "api_key_or_helper_or_custom_endpoint_is_refused": (
        f"{U}/test_policy.py::test_api_key_setting_is_rejected_without_echoing_its_value",
        f"{U}/test_policy.py::test_inherited_auth_or_provider_override_is_rejected_by_name_only",
        f"{U}/test_startup_enforcement.py::test_cli_refuses_to_start_with_an_inherited_api_key",
        f"{I}/test_cli_contract.py::test_inherited_api_key_is_refused",
        f"{U}/test_auth.py::test_api_key_auth_is_rejected",
    ),
    "console_login_or_unknown_auth_schema_or_no_login_blocks_inference": (
        f"{U}/test_auth.py::test_console_auth_is_rejected",
        f"{U}/test_auth.py::test_logged_out_requires_login",
        f"{U}/test_auth.py::test_unknown_auth_status_blocks",
        f"{S}/test_runtime_boundary.py::test_an_authentication_rejection_requires_login",
    ),
    "quota_or_model_failure_never_falls_back": (
        f"{S}/test_runtime_boundary.py::test_a_rate_limit_is_reported_as_quota_exhausted",
        f"{U}/test_dispatch.py::test_terminal_states_map_to_the_parking_exit_codes",
        f"{U}/test_dispatch.py::test_a_crashing_client_is_a_failed_run_not_a_fallback",
        f"{U}/test_service_protocol.py::test_unreachable_service_is_a_failed_run_never_a_fallback",
        f"{U}/test_chat_turns.py::test_a_failed_run_is_an_error_event_and_never_retried_elsewhere",
    ),
    "credential_files_never_enter_a_workspace": (
        f"{I}/test_workspace_import.py::test_import_populates_the_workspace_excludes_denied_files_and_baselines",
        f"{I}/test_workspace_import.py::test_suspected_secrets_block_import_until_each_finding_is_acknowledged",
    ),
    "project_config_cannot_configure_the_trusted_cli": (
        f"{I}/test_cli_contract.py::test_hostile_project_config_is_inert",
        f"{I}/test_cli_contract.py::test_hostile_project_config_is_live_without_the_flags",
    ),
    "built_in_tools_and_ungranted_operations_are_unavailable": (
        f"{S}/test_runtime_boundary.py::test_built_in_tools_cannot_run",
        f"{S}/test_runtime_boundary.py::test_a_tool_that_was_not_granted_cannot_run",
        f"{S}/test_runtime_boundary.py::test_the_credential_store_never_reaches_the_model_or_a_worker",
        f"{U}/test_tool_gateway.py::test_unknown_operation_is_denied",
    ),
    "project_scripts_find_no_credentials_in_their_namespace": (
        f"{S}/test_project_execution.py::test_hostile_project_commands_never_see_controller_secrets",
        f"{S}/test_project_execution.py::test_hostile_build_backend_cannot_reach_the_network",
        f"{S}/test_filesystem_isolation.py::test_process_environment_holds_no_credentials",
    ),
    "stale_cross_project_or_replayed_capability_is_denied": (
        f"{U}/test_tool_gateway.py::test_cross_run_job_is_denied",
        f"{U}/test_tool_gateway.py::test_revoked_capability_cannot_be_replayed",
        f"{U}/test_tool_gateway.py::test_expired_capability_is_denied_and_removed",
        f"{U}/test_tool_gateway.py::test_an_unknown_or_guessed_capability_is_denied",
        f"{U}/test_tool_gateway.py::test_model_supplied_identity_fields_are_rejected",
        f"{S}/test_runtime_boundary.py::test_model_supplied_identity_never_reaches_a_worker",
        f"{U}/test_launcher_server.py::test_unregistered_run_is_refused_without_docker",
    ),
    "links_traversal_and_special_files_are_refused": (
        f"{S}/test_filesystem_isolation.py::test_project_planted_links_are_refused_by_file_operations",
        f"{S}/test_filesystem_isolation.py::test_sibling_workspace_and_host_paths_are_unreachable",
        f"{S}/test_filesystem_isolation.py::test_exported_patch_cannot_smuggle_denied_or_credential_paths",
        f"{I}/test_workspace_import.py::test_hostile_archives_are_refused_and_leave_no_volume",
    ),
    "no_project_command_runs_on_the_controller": (
        f"{S}/test_process_spawn_boundary.py::test_nothing_outside_the_worker_runs_project_commands_through_a_shell",
        f"{S}/test_process_spawn_boundary.py::test_every_spawn_site_is_allowlisted_with_a_category",
        f"{U}/test_deployment_policy.py::test_the_controller_runs_without_a_container_runtime",
        f"{S}/test_project_execution.py::test_unavailable_worker_blocks_instead_of_passing_or_skipping",
    ),
    "worker_has_no_network_daemon_or_host_access": (
        f"{I}/test_launcher.py::test_worker_has_no_network_no_credentials_and_a_read_only_root",
        f"{I}/test_launcher.py::test_running_container_has_the_exact_fixed_security_profile",
        f"{S}/test_filesystem_isolation.py::test_worker_has_no_capabilities_control_socket_or_extra_mounts",
    ),
    "same_host_telemetry_paths_are_denied": (
        f"{S}/test_egress.py::test_telemetry_and_config_calls_on_the_inference_host_are_denied",
        f"{S}/test_egress.py::test_requests_off_the_approved_shape_are_denied",
        f"{S}/test_egress_proxy.py::test_denied_requests_get_a_policy_refusal",
    ),
    "sni_host_ip_redirect_and_rebinding_tricks_are_denied": (
        f"{S}/test_egress.py::test_sni_host_header_and_connect_target_must_agree",
        f"{S}/test_egress.py::test_ip_literals_and_numeric_shorthands_are_denied",
        f"{S}/test_egress.py::test_redirects_are_never_followed",
        f"{S}/test_egress.py::test_rebinding_and_non_public_answers_are_refused",
        f"{S}/test_egress_proxy.py::test_tunnels_to_unapproved_destinations_are_refused",
    ),
    "proxy_outage_bad_policy_or_cli_drift_stops_model_runs": (
        f"{S}/test_egress_proxy.py::test_stopping_the_proxy_blocks_all_traffic",
        f"{S}/test_egress_proxy.py::test_the_proxy_refuses_to_start_with_an_empty_policy",
        f"{S}/test_egress_proxy.py::test_the_proxy_refuses_to_start_with_an_unreviewed_policy",
        f"{S}/test_egress_topology.py::test_the_stack_refuses_to_start_without_the_reviewed_digest",
        f"{S}/test_deployment_images.py::test_unapproved_cli_contract_blocks",
    ),
    "forbidden_features_cannot_be_enabled": (
        f"{U}/test_background_policy.py::test_forbidden_flag_cannot_be_stored_on",
        f"{U}/test_background_policy.py::test_forbidden_flag_cannot_be_switched_on_by_configuration",
        f"{U}/test_policy.py::test_alternate_provider_flag_cannot_be_enabled_through_settings_write",
        f"{S}/test_no_direct_model_clients.py::test_chat_clients_refuse_before_connecting",
    ),
    "partial_timeout_cancel_and_restart_never_report_false_completion": (
        f"{U}/test_stream_parser.py::test_missing_terminal_event_is_a_failure_with_partial_output",
        f"{U}/test_stream_parser.py::test_vendor_max_turns_result_is_blocked_not_completed",
        f"{U}/test_service.py::test_run_time_limit_kills_the_process_group",
        f"{U}/test_service.py::test_cancelling_a_running_run_stops_it_and_revokes_its_tools",
        f"{I}/test_launcher.py::test_hard_timeout_kills_the_container",
        f"{I}/test_launcher.py::test_cancel_removes_the_running_container",
        f"{I}/test_launcher.py::test_reconcile_kills_only_labelled_workers",
        f"{I}/test_local_review_workflow.py::test_unavailable_worker_blocks_capture_and_submit",
        f"{I}/test_local_review_workflow.py::test_repeated_capture_keeps_one_artifact",
    ),
    "forged_public_csrf_wrong_host_or_anonymous_websocket_requests_are_denied": (
        "unit/api/test_access_guard.py::test_an_unlisted_host_is_refused",
        "unit/api/test_access_guard.py::test_a_cross_origin_state_change_with_the_cookie_is_refused",
        "unit/api/test_access_guard.py::test_a_websocket_with_the_cookie_needs_an_allowed_origin",
        f"{S}/test_edge_proxy.py::test_other_hosts_are_refused",
        f"{S}/test_edge_proxy.py::test_browser_identity_headers_never_reach_the_api",
    ),
    "remote_resources_and_project_html_cannot_load_or_run": (
        f"{S}/test_edge_proxy.py::test_panel_responses_carry_a_restrictive_policy",
    ),
    "secret_canaries_never_reach_logs_errors_or_exports": (
        f"{S}/test_log_redaction.py::test_credential_shapes_are_scrubbed_from_messages",
        f"{S}/test_log_redaction.py::test_request_logging_never_prints_credentials_from_the_query_string",
        f"{U}/test_service.py::test_cli_stderr_never_reaches_results_or_logs",
        f"{S}/test_egress_proxy.py::test_proxy_logs_carry_no_request_content",
        f"{I}/test_local_review_workflow.py::test_unsafe_changes_are_never_captured",
    ),
    "legacy_compose_and_quickstart_refuse_to_start": (
        f"{U}/test_deployment_policy.py::test_legacy_compose_controllers_refuse_to_start",
        f"{U}/test_deployment_policy.py::test_the_refusal_entrypoint_exits_non_zero_with_the_message",
        f"{S}/test_acceptance.py::test_the_quickstart_script_refuses_to_start_the_legacy_stack",
    ),
}

# Matrix rows or plan items that code cannot prove, stated so a report never
# claims more than was tested.
NOT_PROVEN_BY_CODE = (
    "a controller, runtime or launcher restart in the middle of a real model job",
    "project HTML or remote images in a real browser (only the response policy is checked)",
    "a full run through the owner's live subscription login",
)
