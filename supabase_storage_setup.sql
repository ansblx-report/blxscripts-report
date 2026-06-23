insert into storage.buckets (id, name, public)
values ('evidencia', 'evidencia', true)
on conflict (id) do update set public = true;

create policy "Public evidence read"
on storage.objects
for select
to public
using (bucket_id = 'evidencia');

create policy "Authenticated evidence upload"
on storage.objects
for insert
to authenticated
with check (bucket_id = 'evidencia');

create policy "Authenticated evidence update"
on storage.objects
for update
to authenticated
using (bucket_id = 'evidencia')
with check (bucket_id = 'evidencia');
