from django.db.migrations.operations.base import Operation


USER_LIFECYCLE_SQL = """
CREATE OR REPLACE FUNCTION skeleton.assert_user_deactivation(target_user uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,skeleton AS $lifecycle$
DECLARE organization uuid;
BEGIN
 IF session_user <> 'skeleton_owner' AND current_setting('skeleton.platform_admin',true) IS DISTINCT FROM 'on' THEN
  RAISE EXCEPTION 'platform control plane required' USING ERRCODE='42501';
 END IF;
 FOR organization IN
  SELECT o.sysid FROM skeleton.sk_tenancy_organization o
  WHERE o.is_active AND o.deleted_at IS NULL AND EXISTS (
   SELECT 1 FROM skeleton.sk_tenancy_membership m
   WHERE m.organization_id=o.sysid AND m.user_id=target_user AND m.is_active AND m.deleted_at IS NULL)
  ORDER BY o.sysid FOR UPDATE
 LOOP
  IF NOT EXISTS (
   SELECT 1 FROM skeleton.sk_tenancy_membership m
   JOIN skeleton.sk_accounts_user u ON u.sysid=m.user_id
   WHERE m.organization_id=organization AND m.user_id <> target_user
    AND m.is_active AND m.deleted_at IS NULL AND u.is_active AND u.deleted_at IS NULL
    AND (
     EXISTS (SELECT 1 FROM skeleton.sk_access_memberrole mr JOIN skeleton.sk_access_role r ON r.sysid=mr.role_id
      WHERE mr.membership_id=m.sysid AND mr.organization_id=organization AND mr.deleted_at IS NULL
       AND r.organization_id=organization AND r.deleted_at IS NULL AND r.is_builtin AND r.code='organization_admin')
     OR EXISTS (SELECT 1 FROM skeleton.sk_access_groupmember gm
      JOIN skeleton.sk_access_accessgroup g ON g.sysid=gm.group_id
      JOIN skeleton.sk_access_grouprole gr ON gr.group_id=g.sysid
      JOIN skeleton.sk_access_role r ON r.sysid=gr.role_id
      WHERE gm.membership_id=m.sysid AND gm.organization_id=organization AND gm.deleted_at IS NULL
       AND g.organization_id=organization AND g.deleted_at IS NULL AND gr.organization_id=organization AND gr.deleted_at IS NULL
       AND r.organization_id=organization AND r.deleted_at IS NULL AND r.is_builtin AND r.code='organization_admin'))
  ) THEN RAISE EXCEPTION 'organization must retain an active administrator' USING ERRCODE='23514'; END IF;
 END LOOP;
END $lifecycle$
"""


class InstallUserLifecycleGuard(Operation):
    reversible = True
    reduces_to_sql = True

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute(USER_LIFECYCLE_SQL)
            schema_editor.execute("ALTER FUNCTION skeleton.assert_user_deactivation(uuid) OWNER TO skeleton_owner")
            schema_editor.execute("REVOKE ALL ON FUNCTION skeleton.assert_user_deactivation(uuid) FROM PUBLIC")
            schema_editor.execute("GRANT EXECUTE ON FUNCTION skeleton.assert_user_deactivation(uuid) TO skeleton_runtime")

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute("DROP FUNCTION IF EXISTS skeleton.assert_user_deactivation(uuid)")

    def describe(self):
        return "Install narrow platform last-administrator identity lifecycle guard"
