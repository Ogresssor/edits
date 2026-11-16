package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"sync"
	"time"

	"github.com/Ogresssor/edits/internal/studio"
)

func printJSON(v any) error {
	b, e := json.MarshalIndent(v, "", "  ")
	if e != nil {
		return e
	}
	fmt.Println(string(b))
	return nil
}
func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if e := run(ctx, os.Args[1:]); e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}
func run(ctx context.Context, args []string) error {
	flags := flag.NewFlagSet("edits", flag.ContinueOnError)
	workspace := flags.String("workspace", ".local/go", "local state directory (separate from Python prototype)")
	flags.Usage = func() {
		fmt.Println("edits [--workspace PATH] doctor|ingest|analyze|render|check|approve|queue|status|batch|demo\nNo automatic uploads. Render/check never publish.")
	}
	if e := flags.Parse(args); e != nil {
		return e
	}
	args = flags.Args()
	if len(args) == 0 {
		flags.Usage()
		return nil
	}
	store, e := studio.NewStore(*workspace)
	if e != nil {
		return e
	}
	switch args[0] {
	case "status":
		q, e := store.Queue()
		if e != nil {
			return e
		}
		return printJSON(q)
	case "approve":
		f := flag.NewFlagSet("approve", flag.ContinueOnError)
		reviewer := f.String("reviewer", "", "reviewer identity")
		watched := f.Bool("reviewed-video-and-audio", false, "full human review completed")
		if e = f.Parse(args[1:]); e != nil {
			return e
		}
		if f.NArg() != 1 {
			return errors.New("approve --reviewer NAME --reviewed-video-and-audio SHA256")
		}
		if e = store.Approve(f.Arg(0), *reviewer, *watched); e == nil {
			fmt.Println("Exact export approved. No upload performed.")
		}
		return e
	case "queue":
		f := flag.NewFlagSet("queue", flag.ContinueOnError)
		at := f.String("at", "", "RFC3339 time with timezone")
		if e = f.Parse(args[1:]); e != nil {
			return e
		}
		if f.NArg() != 1 {
			return errors.New("queue --at TIME SHA256")
		}
		when, err := time.Parse(time.RFC3339, *at)
		if err != nil {
			return err
		}
		if e = store.Enqueue(f.Arg(0), when); e == nil {
			fmt.Println("Local publication plan updated. No upload performed.")
		}
		return e
	}
	binary, e := studio.FindFFmpeg()
	if e != nil {
		return e
	}
	m := studio.Media{Binary: binary, Context: ctx}
	switch args[0] {
	case "doctor":
		fmt.Println("Go:", runtime.Version(), "FFmpeg:", binary)
		_, e = m.Run("", 10*time.Second, "-version")
		return e
	case "ingest":
		if len(args) != 2 {
			return errors.New("ingest SOURCE")
		}
		path, e := filepath.Abs(args[1])
		if e != nil {
			return e
		}
		sha, e := store.SourceHash(path)
		if e != nil {
			return e
		}
		info, e := m.Probe(path)
		if e != nil {
			return e
		}
		target := filepath.Join(store.Root, "assets", sha+".json")
		e = studio.WriteJSON(target, map[string]any{"path": path, "sha256": sha, "info": info, "full_editorial_analysis": false})
		if e == nil {
			fmt.Println(target)
		}
		return e
	case "analyze":
		if len(args) != 2 {
			return errors.New("analyze SOURCE")
		}
		path, e := studio.Analyze(m, store, args[1])
		if e == nil {
			fmt.Println(path)
		}
		return e
	case "render", "batch":
		f := flag.NewFlagSet(args[0], flag.ContinueOnError)
		profile := f.String("profile", "accent", "clean/accent/cinema/intrigue")
		preview := f.Bool("preview", false, "540x960 draft")
		jobs := f.Int("jobs", 1, "bounded batch concurrency, 1..4")
		if e = f.Parse(args[1:]); e != nil {
			return e
		}
		if f.NArg() == 0 {
			return errors.New("render/batch [FLAGS] PLAN.json ...")
		}
		if args[0] == "render" && f.NArg() != 1 {
			return errors.New("render accepts one plan; use batch for multiple")
		}
		if *jobs < 1 || *jobs > 4 {
			return errors.New("jobs must be 1..4")
		}
		return batch(ctx, m, store, f.Args(), *profile, *preview, *jobs)
	case "check":
		if len(args) != 2 {
			return errors.New("check VIDEO")
		}
		file, e := filepath.Abs(args[1])
		if e != nil {
			return e
		}
		q, e := studio.Check(m, store, file)
		if e != nil {
			return e
		}
		if e = printJSON(q); e != nil {
			return e
		}
		if !q.Passed {
			return errors.New("QA failed")
		}
		return nil
	case "demo":
		return studio.BuildMotionDemo(m, store, "docs/assets/demo-scene-v1.png", ".local/motion-demo", ".local/go-motion-demo")
	default:
		return errors.New("unknown command: " + args[0])
	}
}
func batch(ctx context.Context, m studio.Media, s *studio.Store, paths []string, profile string, preview bool, jobs int) error {
	work := make(chan string)
	var wg sync.WaitGroup
	var mu sync.Mutex
	var failures []error
	for i := 0; i < jobs; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for path := range work {
				if ctx.Err() != nil {
					continue
				}
				p, e := studio.ReadPlan(path)
				file := ""
				if e == nil {
					file, e = studio.Render(m, s, p, profile, preview)
				}
				mu.Lock()
				if e != nil {
					failures = append(failures, fmt.Errorf("%s: %w", path, e))
				} else {
					fmt.Println(file)
				}
				mu.Unlock()
			}
		}()
	}
	for _, path := range paths {
		select {
		case work <- path:
		case <-ctx.Done():
			close(work)
			wg.Wait()
			return ctx.Err()
		}
	}
	close(work)
	wg.Wait()
	return errors.Join(failures...)
}
