#!/bin/sh

read -p 'Deleting cluster. Are you sure? [yN] ' reply
if [ "$reply" = "y" -o "$reply" = "yes" ]; then
  kind delete cluster --name example
else
  echo bailout
fi
