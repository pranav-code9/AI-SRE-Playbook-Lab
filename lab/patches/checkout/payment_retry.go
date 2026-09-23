// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0
//
// Lab patch for the AI SRE Playbook: makes checkout's payment-call timeout
// and retry count configurable, so a Helm release can trigger a retry storm.
//
// Copy into src/checkout/ of the OpenTelemetry Demo, then change the single
// payment Charge call in chargeCard to use chargeWithRetry (see README.md).
//
// VERIFY against the pinned demo version: the receiver type (checkout), the
// client field (paymentSvcClient) and the pb import path used by main.go.

package main

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"time"

	pb "github.com/open-telemetry/opentelemetry-demo/src/checkout/genproto/oteldemo"
)

var (
	paymentTimeout = envDuration("PAYMENT_TIMEOUT", 2*time.Second)
	paymentRetries = envInt("PAYMENT_MAX_RETRIES", 0)
)

// chargeWithRetry calls payment's Charge with a per-attempt timeout and up to
// paymentRetries immediate retries. There is deliberately no backoff: that is
// the bug the case study teaches readers to find.
func (cs *checkout) chargeWithRetry(ctx context.Context, req *pb.ChargeRequest) (*pb.ChargeResponse, error) {
	var lastErr error
	for attempt := 0; attempt <= paymentRetries; attempt++ {
		attemptCtx, cancel := context.WithTimeout(ctx, paymentTimeout)
		resp, err := cs.paymentSvcClient.Charge(attemptCtx, req)
		cancel()
		if err == nil {
			return resp, nil
		}
		lastErr = err
	}
	return nil, fmt.Errorf("charge failed after %d attempts: %w", paymentRetries+1, lastErr)
}

func envDuration(name string, fallback time.Duration) time.Duration {
	v := os.Getenv(name)
	if v == "" {
		return fallback
	}
	d, err := time.ParseDuration(v)
	if err != nil || d <= 0 {
		return fallback
	}
	return d
}

func envInt(name string, fallback int) int {
	v := os.Getenv(name)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 {
		return fallback
	}
	return n
}
