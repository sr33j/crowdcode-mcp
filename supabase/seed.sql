insert into services (id, name, directory_slug, stripe_payee_ref)
values
  ('svc_code_review', 'Code Review Agent', 'code-review-agent', 'acct_demo_code_review'),
  ('svc_doc_writer', 'Documentation Writer', 'documentation-writer', 'acct_demo_doc_writer'),
  ('svc_test_runner', 'Test Runner Agent', 'test-runner-agent', 'acct_demo_test_runner')
on conflict (id) do update
set
  name = excluded.name,
  directory_slug = excluded.directory_slug,
  stripe_payee_ref = excluded.stripe_payee_ref;

