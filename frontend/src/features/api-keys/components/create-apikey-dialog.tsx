"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2 } from "lucide-react";
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
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<CreateApiKeyInput>({
    resolver: zodResolver(createApiKeySchema),
  });

  function handleOpenChange(value: boolean) {
    if (!value) reset();
    onOpenChange(value);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="border-white/5 bg-zinc-950 sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add API Key</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="label">Label</Label>
            <Input
              id="label"
              placeholder="My Bitfinex Key"
              {...register("label")}
            />
            {errors.label && (
              <p className="text-xs text-rose-500">{errors.label.message}</p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="apiKey">API Key</Label>
            <Input
              id="apiKey"
              placeholder="Enter your Bitfinex API key"
              className="font-mono"
              {...register("apiKey")}
            />
            {errors.apiKey && (
              <p className="text-xs text-rose-500">{errors.apiKey.message}</p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="apiSecret">API Secret</Label>
            <Input
              id="apiSecret"
              type="password"
              placeholder="Enter your Bitfinex API secret"
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
              Cancel
            </Button>
            <Button type="submit" disabled={isPending}>
              {isPending && <Loader2 className="mr-1.5 size-4 animate-spin" />}
              Add Key
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
