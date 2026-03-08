export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-zinc-950">
      <div className="w-full max-w-sm rounded-xl border border-white/5 bg-white/[0.02] p-8 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)] backdrop-blur-xl">
        {children}
      </div>
    </div>
  );
}
