"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { type CreateApiKeyInput, createApiKeySchema } from "@/lib/validations";

interface CreateApiKeyDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (data: CreateApiKeyInput) => void;
  isPending: boolean;
}

export function CreateApiKeyDialog({
  open,
  onOpenChange,
  onSubmit,
  isPending,
}: CreateApiKeyDialogProps) {
  const t = useTranslations("apiKeys");
  const tc = useTranslations("common");
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<CreateApiKeyInput>({
    resolver: zodResolver(createApiKeySchema),
  });

  useEffect(() => {
    if (open) reset();
  }, [open, reset]);

  function handleOpenChange(value: boolean) {
    if (!value) reset();
    onOpenChange(value);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="border-white/5 bg-zinc-950 sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("addKey")}</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="label">{t("label")}</Label>
            <Input
              id="label"
              placeholder={t("labelPlaceholder")}
              {...register("label")}
            />
            {errors.label && (
              <p className="text-xs text-rose-500">{errors.label.message}</p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="apiKey">{t("apiKey")}</Label>
            <Input
              id="apiKey"
              placeholder={t("apiKeyPlaceholder")}
              className="font-mono"
              {...register("apiKey")}
            />
            {errors.apiKey && (
              <p className="text-xs text-rose-500">{errors.apiKey.message}</p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="apiSecret">{t("apiSecret")}</Label>
            <Input
              id="apiSecret"
              type="password"
              placeholder={t("apiSecretPlaceholder")}
              className="font-mono"
              {...register("apiSecret")}
            />
            {errors.apiSecret && (
              <p className="text-xs text-rose-500">
                {errors.apiSecret.message}
              </p>
            )}
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => handleOpenChange(false)}
            >
              {tc("cancel")}
            </Button>
            <Button type="submit" disabled={isPending}>
              {isPending && <Loader2 className="mr-1.5 size-4 animate-spin" />}
              {t("addKeyShort")}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
